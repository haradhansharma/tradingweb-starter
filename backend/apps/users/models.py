from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    # Add giant-project essentials now so they are in the DB
    is_verified = models.BooleanField(default=False)
    bio = models.TextField(max_length=500, blank=True)
    reputation = models.IntegerField(default=0)
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    tracked_assets = models.JSONField(default=list, blank=True)
    risk_tolerance = models.CharField(
        max_length=20,
        choices=[
            ("conservative", "Conservative"),
            ("moderate", "Moderate"),
            ("aggressive", "Aggressive"),
        ],
        default="moderate",
    )

    # Notification preferences
    notify_strong_signals = models.BooleanField(default=True)
    notify_whale_activity = models.BooleanField(default=True)
    notify_market_shifts = models.BooleanField(default=True)

    def __str__(self):

        return self.username
