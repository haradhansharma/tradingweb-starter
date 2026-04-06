# backend/apps/common/admin.py

from django.contrib import admin
from django.contrib.sites.models import Site
from django.contrib.sites.admin import SiteAdmin

from .models import CustomSite, SiteSettings


class CustomSiteAdmin(SiteAdmin):
    list_display = ["domain", "name", "site_title", "support_email", "maintenance_mode"]
    fieldsets = (
        (None, {"fields": ("domain", "name")}),
        (
            "Custom fields",
            {"fields": ("site_title", "support_email", "maintenance_mode")},
        ),
    )


class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ["site", "allow_registration", "max_users"]
    list_select_related = ["site"]


admin.site.unregister(Site)
admin.site.register(CustomSite, CustomSiteAdmin)
admin.site.register(SiteSettings, SiteSettingsAdmin)
