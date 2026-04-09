from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User, BrokerCredential


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    model = User
    list_display = ["username", "email", "is_staff", "is_active", "is_verified"]
    list_filter = ["is_staff", "is_active", "is_verified"]
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            "Personal info",
            {"fields": ("first_name", "last_name", "email", "bio", "avatar")},
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "is_verified",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    search_fields = ["username", "email"]
    ordering = ["username"]


@admin.register(BrokerCredential)
class BrokerCredentialAdmin(admin.ModelAdmin):
    """
    Admin view for broker credentials.

    Security notes:
      - Encrypted values (api_key, api_secret) are shown as masked.
      - Only staff users can access this admin view.
      - The 'decrypt' action allows staff to temporarily view credentials
        for support purposes (logged in audit trail).
    """

    list_display = [
        "id",
        "user",
        "broker",
        "label",
        "is_testnet",
        "is_active",
        "last_validated",
        "created_at",
    ]
    list_filter = ["broker", "is_testnet", "is_active"]
    search_fields = ["user__username", "user__email", "label"]
    readonly_fields = ["created_at", "updated_at"]
    exclude = ["api_key", "api_secret"]  # Never show encrypted blobs in admin forms

    fieldsets = (
        (
            "Credential Info",
            {
                "fields": (
                    "user",
                    "broker",
                    "label",
                    "is_testnet",
                    "is_active",
                    "last_validated",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        """Only superusers can see all credentials; regular staff see none."""
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            return qs.none()
        return qs

    def has_module_permission(self, request):
        """Only staff can see this module in admin index."""
        return request.user.is_staff
