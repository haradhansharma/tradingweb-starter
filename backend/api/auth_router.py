"""
User API Router
===============
Handles authentication and user-specific endpoints.

Auth flow:
  - POST /api/auth/register  → create account
  - POST /api/auth/pair      → obtain JWT pair (access + refresh)
  - POST /api/auth/refresh   → refresh access token
  - POST /api/auth/verify    → verify token validity
  - GET  /api/auth/me        → current user profile (JWT required)

Broker credentials (JWT required):
  - GET    /api/auth/credentials          → list user's credentials
  - POST   /api/auth/credentials          → save new credentials
  - PUT    /api/auth/credentials/{id}     → update credentials
  - DELETE /api/auth/credentials/{id}     → delete credentials
"""

from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from ninja import NinjaAPI, Router, Schema
from ninja.security import HttpBearer
from pydantic import field_validator, model_validator

from ninja_jwt.controller import NinjaJWTDefaultController
from ninja_jwt.authentication import JWTAuth
from ninja_jwt.tokens import RefreshToken, AccessToken
from ninja_jwt.schema import (
    TokenObtainPairInputSchema,
    TokenObtainPairOutputSchema,
    TokenRefreshInputSchema,
    TokenRefreshOutputSchema,
)

from users.models import BrokerCredential
from common.broker_config import BROKER_CONFIGS

User = get_user_model()

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
    """Registration success response."""
    id: int
    username: str
    email: str


class MessageSchema(Schema):
    """Generic message response."""
    message: str


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
# REGISTRATION
# =============================================================================

@auth_router.post(
    "/register",
    response={201: RegisterOutputSchema, 400: MessageSchema},
    auth=None,
)
def register(request, data: RegisterInputSchema):
    """Create a new user account. Returns user info on success."""
    # Check if registration is allowed
    from common.models import SiteSettings
    try:
        from django.contrib.sites.models import Site
        site_settings = getattr(Site.objects.get_current(), "settings", None)
        if site_settings and not site_settings.allow_registration:
            return 400, {"message": "Registration is currently disabled."}
    except Exception:
        pass

    try:
        user = User.objects.create_user(
            username=data.username,
            email=data.email,
            password=data.password,
        )
        return 201, {
            "id": user.id,
            "username": user.username,
            "email": user.email,
        }
    except IntegrityError:
        return 400, {"message": "Username or email already exists."}


# =============================================================================
# TOKEN OBTAIN (uses ninja_jwt built-in schema)
# =============================================================================

@auth_router.post(
    "/pair",
    response=TokenObtainPairOutputSchema,
    auth=None,
)
def obtain_token(request, user_token: TokenObtainPairInputSchema):
    """
    Obtain JWT token pair.
    Send { "username": "...", "password": "..." }.
    Returns { "access": "...", "refresh": "...", "username": "..." }.
    """
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
