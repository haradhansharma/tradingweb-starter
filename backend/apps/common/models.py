# from django.conf import settings
from django.contrib.sites.models import Site
from django.db import models


class CustomSite(Site):
    """Custom site model extending Django's built-in Site."""

    # Example additional fields
    site_title = models.CharField(max_length=255, blank=True, default="")
    support_email = models.EmailField(blank=True, null=True)
    maintenance_mode = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Custom Site"
        verbose_name_plural = "Custom Sites"

    def __str__(self):
        return f"{self.domain} ({self.site_title or 'No title'})"


class SiteSettings(models.Model):
    """Site-specific settings attached to the Django sites framework."""

    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="settings")
    homepage_description = models.TextField(blank=True)
    allow_registration = models.BooleanField(default=True)
    max_users = models.PositiveIntegerField(default=0, help_text="0 means unlimited")

    class Meta:
        verbose_name = "Site Setting"
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return f"Settings for {self.site.domain}"
