"""
User API Router
===============
Handles authentication and user-specific endpoints.

Auth flow:
  1. POST /api/auth/register   → create account (is_active=False), send OTP email
  2. POST /api/auth/verify-otp  → verify OTP code, activate account
  3. POST /api/auth/resend-otp  → resend OTP code (rate limited)
  4. POST /api/auth/pair        → obtain JWT pair (access + refresh) — requires is_active
  5. POST /api/auth/refresh     → refresh access token
  6. GET  /api/auth/me          → current user profile (JWT required)
  7. POST /api/auth/change-password → change password (JWT required)
  8. POST /api/auth/forgot-password → request password reset OTP (public)
  9. POST /api/auth/reset-password  → reset password with OTP (public)

Broker credentials (JWT required):
  - GET    /api/auth/credentials          → list user's credentials
  - POST   /api/auth/credentials          → save new credentials
  - PUT    /api/auth/credentials/{id}     → update credentials
  - DELETE /api/auth/credentials/{id}     → delete credentials
"""

import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model, authenticate
from django.db import IntegrityError

from ninja import Router, Schema
from pydantic import field_validator

from ninja_jwt.authentication import JWTAuth
from ninja_jwt.schema import (
    TokenObtainPairInputSchema,
    TokenObtainPairOutputSchema,
    TokenRefreshInputSchema,
    TokenRefreshOutputSchema,
)

from users.models import BrokerCredential
from users.otp_utils import create_otp, send_otp_email, verify_otp
from common.broker_config import BROKER_CONFIGS

User = get_user_model()
logger = logging.getLogger("django.request")

# ─── JWT Auth backend for protecting endpoints ───

class JWTAuthBackend(JWTAuth):
    """JWT Bearer authentication for Django Ninja endpoints."""

    openapi_scheme = "bearer"


jwt_auth = JWTAuthBackend()


# ─── Schemas ───

class RegisterInputSchema(Schema):
    """Registration input with validation."""
    username: str
    email: str
    password: str

    @field_validator("username")
    @classmethod
    def username_min_length(cls, v: str) -> str:
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        if not v.isalnum() and "_" not in v and "-" not in v:
            raise ValueError("Username can only contain letters, digits, underscores, and hyphens")
        return v.lower()

    @field_validator("email")
    @classmethod
    def email_format(cls, v: str) -> str:
        if "@" not in v or "." not in v:
            raise ValueError("Invalid email format")
        return v.lower()

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class RegisterOutputSchema(Schema):
    """Registration success response — user needs OTP verification."""
    id: int
    username: str
    email: str
    message: str


class VerifyOtpInputSchema(Schema):
    """OTP verification input."""
    username: str
    code: str

    @field_validator("code")
    @classmethod
    def code_format(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 6:
            raise ValueError("OTP must be a 6-digit number")
        return v


class ResendOtpInputSchema(Schema):
    """Resend OTP input."""
    username: str


class MessageSchema(Schema):
    """Generic message response."""
    message: str


class VerifyOtpOutputSchema(Schema):
    """OTP verification result."""
    success: bool
    message: str
    reason: Optional[str] = None
    remaining_attempts: Optional[int] = None


class UserOutputSchema(Schema):
    """Current user profile output."""
    id: int
    username: str
    email: Optional[str] = None
    is_verified: bool
    bio: Optional[str] = None
    risk_tolerance: Optional[str] = None
    tracked_assets: list = []
    notify_strong_signals: bool = True
    notify_whale_activity: bool = True
    notify_market_shifts: bool = True


class UserUpdateInputSchema(Schema):
    """User profile update input (all optional)."""
    bio: Optional[str] = None
    risk_tolerance: Optional[str] = None
    tracked_assets: Optional[list] = None
    notify_strong_signals: Optional[bool] = None
    notify_whale_activity: Optional[bool] = None
    notify_market_shifts: Optional[bool] = None

    @field_validator("risk_tolerance")
    @classmethod
    def valid_risk(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("conservative", "moderate", "aggressive"):
            raise ValueError("Must be conservative, moderate, or aggressive")
        return v


class ChangePasswordInputSchema(Schema):
    """Change password input (requires current password)."""
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("New password must be at least 8 characters")
        return v


class ForgotPasswordInputSchema(Schema):
    """Forgot password — request OTP by username or email."""
    username: Optional[str] = None
    email: Optional[str] = None


class ResetPasswordInputSchema(Schema):
    """Reset password with OTP."""
    username: str
    code: str
    new_password: str

    @field_validator("code")
    @classmethod
    def code_format(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 6:
            raise ValueError("OTP must be a 6-digit number")
        return v

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("New password must be at least 8 characters")
        return v


class CredentialInputSchema(Schema):
    """Input for saving broker credentials."""
    broker: str
    label: str = ""
    api_key: str
    api_secret: str
    is_testnet: bool = False

    @field_validator("broker")
    @classmethod
    def valid_broker(cls, v: str) -> str:
        if v.lower() not in BROKER_CONFIGS:
            available = ", ".join(BROKER_CONFIGS.keys())
            raise ValueError(f"Unknown broker. Available: {available}")
        return v.lower()


class CredentialOutputSchema(Schema):
    """Output for credential listing (never includes secrets)."""
    id: int
    broker: str
    label: str
    is_testnet: bool
    is_active: bool
    last_validated: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CredentialUpdateSchema(Schema):
    """Input for updating existing credentials."""
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    label: Optional[str] = None
    is_testnet: Optional[bool] = None
    is_active: Optional[bool] = None


# ─── Router Setup ───

auth_router = Router(tags=["auth"])


# =============================================================================
# REGISTRATION (creates inactive user, sends OTP)
# =============================================================================

@auth_router.post(
    "/register",
    response={201: RegisterOutputSchema, 400: MessageSchema},
    auth=None,
)
def register(request, data: RegisterInputSchema):
    """
    Create a new user account (inactive until OTP verified).
    Sends a 6-digit verification code to the user's email.
    Returns user info on success.
    """
    # Check if registration is allowed
    from common.models import SiteSettings
    try:
        from django.contrib.sites.models import Site
        site_settings = getattr(Site.objects.get_current(), "settings", None)
        if site_settings and not site_settings.allow_registration:
            return 400, {"message": "Registration is currently disabled."}
    except Exception:
        pass

    # Explicit duplicate checks (email is not unique by default in AbstractUser)
    if User.objects.filter(username=data.username).exists():
        return 400, {"message": "Username already exists."}
    if User.objects.filter(email=data.email).exists():
        return 400, {"message": "Email already exists."}

    try:
        user = User.objects.create_user(
            username=data.username,
            email=data.email,
            password=data.password,
            is_active=False,  # Inactive until OTP verified
        )
    except IntegrityError:
        return 400, {"message": "Username or email already exists."}

    # Generate and send OTP
    try:
        otp = create_otp(user, purpose="registration")
        send_otp_email(user, otp)
        logger.info(f"OTP sent to {user.email} for registration of {user.username}")
    except ValueError as e:
        # Rate limit hit — delete user and report
        user.delete()
        return 400, {"message": str(e)}
    except Exception as e:
        logger.error(f"Failed to send OTP email: {e}")
        user.delete()
        return 400, {"message": "Failed to send verification email. Please try again later."}

    return 201, {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "message": "Verification code sent to your email. Please check your inbox.",
    }


# =============================================================================
# OTP VERIFICATION (activates user account)
# =============================================================================

@auth_router.post(
    "/verify-otp",
    response={200: VerifyOtpOutputSchema, 400: VerifyOtpOutputSchema},
    auth=None,
)
def verify_otp_endpoint(request, data: VerifyOtpInputSchema):
    """
    Verify OTP code to activate user account.
    On success: user.is_active = True, user.is_verified = True.
    """
    try:
        user = User.objects.get(username=data.username.lower())
    except User.DoesNotExist:
        return 400, VerifyOtpOutputSchema(
            success=False,
            message="User not found.",
            reason="user_not_found",
        )

    if user.is_active and user.is_verified:
        return 200, VerifyOtpOutputSchema(
            success=True,
            message="Account is already verified. You can sign in.",
        )

    result = verify_otp(user, data.code, purpose="registration")
    return 200 if result["success"] else 400, VerifyOtpOutputSchema(**result)


# =============================================================================
# RESEND OTP (rate limited)
# =============================================================================

@auth_router.post(
    "/resend-otp",
    response={200: MessageSchema, 400: MessageSchema},
    auth=None,
)
def resend_otp_endpoint(request, data: ResendOtpInputSchema):
    """
    Resend OTP verification code.
    Rate limited to 3 requests per hour.
    """
    try:
        user = User.objects.get(username=data.username.lower())
    except User.DoesNotExist:
        return 400, {"message": "User not found."}

    if user.is_active and user.is_verified:
        return 400, {"message": "Account is already verified. You can sign in."}

    try:
        otp = create_otp(user, purpose="registration")
        sent = send_otp_email(user, otp)
        if sent:
            return 200, {"message": "New verification code sent to your email."}
        else:
            return 400, {"message": "Failed to send verification email. Please try again."}
    except ValueError as e:
        return 400, {"message": str(e)}
    except Exception as e:
        logger.error(f"Resend OTP failed: {e}")
        return 400, {"message": "An error occurred. Please try again later."}


# =============================================================================
# TOKEN OBTAIN (login — requires active user)
# =============================================================================

@auth_router.post(
    "/pair",
    response={200: TokenObtainPairOutputSchema, 403: MessageSchema},
    auth=None,
)
def obtain_token(request, user_token: TokenObtainPairInputSchema):
    """
    Obtain JWT token pair.
    Send { "username": "...", "password": "..." }.
    Returns { "access": "...", "refresh": "..." }.

    Uses Django's authenticate() via ninja_jwt's built-in mechanism.
    For inactive (unverified) users, returns a helpful 403 message
    before even attempting password authentication.
    """
    # Pre-check: if user exists but is not active, give helpful message
    # (we do NOT check the password here — that would bypass Django conventions)
    try:
        user = User.objects.get(username=user_token.username)
        if not user.is_active:
            return 403, {
                "message": "Account is not verified. Please check your email for the verification code.",
                "requires_verification": True,
            }
    except User.DoesNotExist:
        pass  # User doesn't exist — let authenticate() handle it

    # Use ninja_jwt's built-in authentication (calls Django's authenticate() internally)
    user_token.check_user_authentication_rule()
    return user_token.to_response_schema()


# =============================================================================
# TOKEN REFRESH
# =============================================================================

@auth_router.post(
    "/refresh",
    response=TokenRefreshOutputSchema,
    auth=None,
)
def refresh_token(request, refresh_token: TokenRefreshInputSchema):
    """
    Refresh access token.
    Send { "refresh": "..." }.
    Returns { "access": "..." }.
    """
    return refresh_token.to_response_schema()


# =============================================================================
# CURRENT USER PROFILE
# =============================================================================

@auth_router.get("/me", response=UserOutputSchema, auth=jwt_auth)
def get_me(request):
    """Return the current authenticated user's profile."""
    user = request.auth  # Set by JWTAuth
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_verified": user.is_verified,
        "bio": user.bio,
        "risk_tolerance": user.risk_tolerance,
        "tracked_assets": user.tracked_assets,
        "notify_strong_signals": user.notify_strong_signals,
        "notify_whale_activity": user.notify_whale_activity,
        "notify_market_shifts": user.notify_market_shifts,
    }


@auth_router.put("/me", response=UserOutputSchema, auth=jwt_auth)
def update_me(request, data: UserUpdateInputSchema):
    """Update the current authenticated user's profile."""
    user = request.auth
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(user, key, value)
    user.save(update_fields=list(update_data.keys()))

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_verified": user.is_verified,
        "bio": user.bio,
        "risk_tolerance": user.risk_tolerance,
        "tracked_assets": user.tracked_assets,
        "notify_strong_signals": user.notify_strong_signals,
        "notify_whale_activity": user.notify_whale_activity,
        "notify_market_shifts": user.notify_market_shifts,
    }


# =============================================================================
# CHANGE PASSWORD (JWT required)
# =============================================================================

@auth_router.post(
    "/change-password",
    response={200: MessageSchema, 400: MessageSchema},
    auth=jwt_auth,
)
def change_password(request, data: ChangePasswordInputSchema):
    """Change the authenticated user's password. Requires current password."""
    user = request.auth

    if not user.check_password(data.current_password):
        return 400, {"message": "Current password is incorrect."}

    user.set_password(data.new_password)
    user.save(update_fields=["password"])

    return 200, {"message": "Password changed successfully."}


# =============================================================================
# FORGOT PASSWORD (request OTP)
# =============================================================================

@auth_router.post(
    "/forgot-password",
    response={200: MessageSchema, 400: MessageSchema},
    auth=None,
)
def forgot_password(request, data: ForgotPasswordInputSchema):
    """
    Request a password reset OTP.
    User identifies by username or email.
    Always returns 200 to avoid user enumeration.
    """
    user = None
    if data.username:
        try:
            user = User.objects.get(username=data.username.lower(), is_active=True)
        except User.DoesNotExist:
            pass
    elif data.email:
        try:
            user = User.objects.get(email=data.email.lower(), is_active=True)
        except User.DoesNotExist:
            pass

    if user:
        try:
            otp = create_otp(user, purpose="password_reset")
            send_otp_email(user, otp)
        except Exception as e:
            logger.error(f"Forgot password OTP failed: {e}")

    # Always return 200 to prevent user enumeration
    return 200, {
        "message": "If an account with that username/email exists, a verification code has been sent.",
    }


# =============================================================================
# RESET PASSWORD (with OTP)
# =============================================================================

@auth_router.post(
    "/reset-password",
    response={200: MessageSchema, 400: MessageSchema},
    auth=None,
)
def reset_password(request, data: ResetPasswordInputSchema):
    """Reset password using OTP verification code."""
    try:
        user = User.objects.get(username=data.username.lower(), is_active=True)
    except User.DoesNotExist:
        return 400, {"message": "User not found."}

    result = verify_otp(user, data.code, purpose="password_reset")
    if not result["success"]:
        return 400, MessageSchema(message=result["message"])

    # OTP is valid — set new password
    user.set_password(data.new_password)
    user.save(update_fields=["password"])

    return 200, {"message": "Password reset successfully. You can now sign in with your new password."}


# =============================================================================
# BROKER CREDENTIALS (JWT required)
# =============================================================================

@auth_router.get("/credentials", response=list[CredentialOutputSchema], auth=jwt_auth)
def list_credentials(request):
    """List all broker credentials for the authenticated user."""
    user = request.auth
    creds = BrokerCredential.objects.filter(user=user).order_by("-created_at")
    return [
        {
            "id": c.id,
            "broker": c.broker,
            "label": c.label,
            "is_testnet": c.is_testnet,
            "is_active": c.is_active,
            "last_validated": c.last_validated.isoformat() if c.last_validated else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in creds
    ]


@auth_router.post(
    "/credentials",
    response={201: CredentialOutputSchema, 400: MessageSchema},
    auth=jwt_auth,
)
def create_credential(request, data: CredentialInputSchema):
    """Save new broker credentials (encrypted at rest)."""
    user = request.auth
    try:
        cred = BrokerCredential(
            user=user,
            broker=data.broker,
            label=data.label,
            is_testnet=data.is_testnet,
        )
        cred.set_credentials(data.api_key, data.api_secret)
        cred.full_clean()  # Runs model.clean() for broker validation
        cred.save()
        return 201, {
            "id": cred.id,
            "broker": cred.broker,
            "label": cred.label,
            "is_testnet": cred.is_testnet,
            "is_active": cred.is_active,
            "last_validated": None,
            "created_at": cred.created_at.isoformat() if cred.created_at else None,
            "updated_at": cred.updated_at.isoformat() if cred.updated_at else None,
        }
    except IntegrityError:
        return 400, {"message": "Credentials with this broker and label already exist."}
    except Exception as e:
        return 400, {"message": str(e)}


@auth_router.put(
    "/credentials/{credential_id}",
    response={200: CredentialOutputSchema, 404: MessageSchema, 400: MessageSchema},
    auth=jwt_auth,
)
def update_credential(request, credential_id: int, data: CredentialUpdateSchema):
    """Update existing broker credentials."""
    user = request.auth
    try:
        cred = BrokerCredential.objects.get(id=credential_id, user=user)
    except BrokerCredential.DoesNotExist:
        return 404, {"message": "Credential not found."}

    update_data = data.model_dump(exclude_unset=True)

    # Handle credential encryption separately
    if "api_key" in update_data or "api_secret" in update_data:
        current = cred.get_credentials()
        new_key = update_data.pop("api_key", current["api_key"])
        new_secret = update_data.pop("api_secret", current["api_secret"])
        cred.set_credentials(new_key, new_secret)

    for key, value in update_data.items():
        setattr(cred, key, value)

    cred.save()

    return 200, {
        "id": cred.id,
        "broker": cred.broker,
        "label": cred.label,
        "is_testnet": cred.is_testnet,
        "is_active": cred.is_active,
        "last_validated": cred.last_validated.isoformat() if cred.last_validated else None,
        "created_at": cred.created_at.isoformat() if cred.created_at else None,
        "updated_at": cred.updated_at.isoformat() if cred.updated_at else None,
    }


@auth_router.delete(
    "/credentials/{credential_id}",
    response={200: MessageSchema, 404: MessageSchema},
    auth=jwt_auth,
)
def delete_credential(request, credential_id: int):
    """Delete broker credentials."""
    user = request.auth
    try:
        cred = BrokerCredential.objects.get(id=credential_id, user=user)
    except BrokerCredential.DoesNotExist:
        return 404, {"message": "Credential not found."}

    cred.delete()
    return 200, {"message": "Credential deleted."}
