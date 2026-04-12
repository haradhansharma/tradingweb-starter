"""
OTP Utility Functions
=====================
Handles OTP generation, sending, and verification for user registration.

Architecture:
  - 6-digit numeric code
  - Configurable expiry (default 10 minutes)
  - Max 5 attempts per code
  - Rate limiting: max 3 OTP sends per user per hour
  - Uses Django's email backend (SMTP in production, console in DEBUG)
"""

import random
import string
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.contrib.auth import get_user_model

from users.models import OTPVerification

User = get_user_model()

# ── Configuration ──

OTP_LENGTH = 6
OTP_EXPIRY_SECONDS = getattr(settings, "OTP_EXPIRY_SECONDS", 600)  # 10 minutes
OTP_MAX_ATTEMPTS = getattr(settings, "OTP_MAX_ATTEMPTS", 5)
OTP_RATE_LIMIT = getattr(settings, "OTP_RATE_LIMIT", 3)  # max sends per hour
OTP_RATE_WINDOW_SECONDS = 3600  # 1 hour


def generate_otp_code(length: int = OTP_LENGTH) -> str:
    """Generate a random numeric OTP code."""
    return "".join(random.choices(string.digits, k=length))


def create_otp(user: User, purpose: str = "registration") -> OTPVerification:
    """
    Create a new OTP for a user.
    Invalidates all existing unused OTPs of the same purpose.
    Enforces rate limiting (max OTP_RATE_LIMIT sends per hour).
    Returns the created OTP instance.
    Raises ValueError if rate limit exceeded.
    """
    # Rate limiting check
    window_start = timezone.now() - timedelta(seconds=OTP_RATE_WINDOW_SECONDS)
    recent_otps = OTPVerification.objects.filter(
        user=user,
        purpose=purpose,
        created_at__gte=window_start,
    )
    if recent_otps.count() >= OTP_RATE_LIMIT:
        raise ValueError(
            f"Too many OTP requests. Please wait {OTP_RATE_WINDOW_SECONDS // 60} minutes "
            f"before requesting another code."
        )

    # Invalidate existing unused OTPs of same purpose
    OTPVerification.objects.filter(
        user=user,
        purpose=purpose,
        is_used=False,
    ).delete()

    code = generate_otp_code()
    otp = OTPVerification.objects.create(
        user=user,
        code=code,
        purpose=purpose,
        expires_at=timezone.now() + timedelta(seconds=OTP_EXPIRY_SECONDS),
    )
    return otp


def send_otp_email(user: User, otp: OTPVerification) -> bool:
    """
    Send OTP code to user's email address.
    Returns True if email was sent successfully.
    Uses HTML email template for professional appearance.
    """
    if not user.email:
        raise ValueError("User has no email address configured.")

    context = {
        "user": user,
        "otp": otp,
        "code": otp.code,
        "expiry_minutes": OTP_EXPIRY_SECONDS // 60,
        "site_name": "MarketPulse",
    }

    subject = f"Your MarketPulse Verification Code: {otp.code}"

    try:
        send_mail(
            subject=subject,
            message=f"Your verification code is: {otp.code}\n\n"
                    f"This code expires in {OTP_EXPIRY_SECONDS // 60} minutes.\n\n"
                    f"If you did not request this code, please ignore this email.",
            html_message=render_to_string("users/email/otp_email.html", context),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@marketpulse.com"),
            recipient_list=[user.email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        import logging
        logger = logging.getLogger("django.request")
        logger.error(f"Failed to send OTP email to {user.email}: {e}")
        return False


def verify_otp(user: User, code: str, purpose: str = "registration") -> dict:
    """
    Verify an OTP code for a user.

    Returns dict:
      - {"success": True, "message": "..."} on success
      - {"success": False, "message": "...", "reason": "..."} on failure

    On successful verification:
      - OTP is marked as used
      - For registration purpose: user.is_active = True, user.is_verified = True
    """
    # Find the most recent unused OTP for this user and purpose
    try:
        otp = OTPVerification.objects.filter(
            user=user,
            purpose=purpose,
            is_used=False,
        ).order_by("-created_at").first()
    except Exception:
        return {
            "success": False,
            "message": "Verification failed. Please try again.",
            "reason": "db_error",
        }

    if not otp:
        return {
            "success": False,
            "message": "No active verification code found. Please request a new one.",
            "reason": "no_otp",
        }

    # Check expiry first
    if otp.is_expired:
        return {
            "success": False,
            "message": "Verification code has expired. Please request a new one.",
            "reason": "expired",
        }

    # Check max attempts
    if otp.attempts >= otp.max_attempts:
        return {
            "success": False,
            "message": "Too many failed attempts. Please request a new code.",
            "reason": "max_attempts",
        }

    # Increment attempts
    otp.increment_attempts()

    # Compare codes
    if otp.code != code.strip():
        remaining = otp.max_attempts - otp.attempts
        return {
            "success": False,
            "message": f"Invalid verification code. {remaining} attempt{'s' if remaining != 1 else ''} remaining.",
            "reason": "invalid_code",
            "remaining_attempts": remaining,
        }

    # ── Code is correct ──
    otp.mark_used()

    # For registration: activate user
    if purpose == "registration":
        user.is_active = True
        user.is_verified = True
        user.save(update_fields=["is_active", "is_verified"])

    return {
        "success": True,
        "message": "Verification successful! Your account is now active.",
    }
