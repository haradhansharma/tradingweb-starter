"""
Django Template Views
=====================
Server-side rendered pages using Django templates with @login_required.
These coexist alongside the Astro SPA — Django admin, user profile,
and broker credential management use these templates.
"""

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout
from django.contrib import messages
from django.conf import settings


def login_view(request):
    """
    Login page — renders the Django login template.
    Uses Django's built-in session auth for template-based pages.
    The Astro frontend uses JWT instead (separate auth flow).
    """
    return render(request, "registration/login.html")


@login_required
def dashboard_template_view(request):
    """
    A simple template-based dashboard wrapper.
    Useful for embedding the Astro SPA via iframe or as a fallback
    when JavaScript is disabled.
    """
    return render(request, "registration/dashboard.html", {"user": request.user})


@login_required
def profile_view(request):
    """User profile page — view and update profile settings."""
    return render(request, "registration/profile.html", {"user": request.user})


@login_required
def credentials_view(request):
    """Broker credentials management page."""
    from apps.common.broker_config import get_available_brokers

    return render(
        request,
        "registration/credentials.html",
        {
            "user": request.user,
            "available_brokers": get_available_brokers(),
        },
    )


def logout_view(request):
    """Logout and redirect to login."""
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect(settings.LOGIN_URL)
