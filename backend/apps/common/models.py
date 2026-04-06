from django.contrib.sites.models import Site
from django.db import models


class CustomSite(Site):
    """Custom site model extending Django's built-in Site."""

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


class IntelligenceSnapshot(models.Model):
    """
    Optional: Store historical intelligence snapshots for analysis.
    """

    asset = models.CharField(max_length=10, db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    # Metrics
    pcr = models.FloatField()
    max_pain = models.FloatField()
    call_resistance = models.FloatField()
    put_support = models.FloatField()
    avg_iv = models.FloatField()
    whale_delta = models.FloatField()
    index_price = models.FloatField()

    # Decision
    action = models.CharField(max_length=20)
    score = models.FloatField()

    # Raw data (for debugging)
    raw_data = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["asset", "-timestamp"]),
        ]

    def __str__(self):
        return f"{self.asset} @ {self.timestamp}: {self.action}"
