from django.contrib.auth.models import AbstractUser
from django.db import models
from django.core.exceptions import ValidationError
from cryptography.fernet import Fernet
from django.conf import settings



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



class BrokerCredential(models.Model):
    """
    Per-user broker API credentials.

    Credentials are encrypted at rest using Fernet (AES-128-CBC) with a
    server-side encryption key derived from Django's SECRET_KEY via
    PBKDF2. Only the owning user (or staff) can read decrypted values.

    Architecture note:
      - Public market data (OI, markPrice, trades, intelligence) does NOT
        require user credentials — it uses server-level env vars.
      - User credentials are only needed for order-placement actions
        (future feature), which will read from this model.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="broker_credentials",
    )
    broker = models.CharField(
        max_length=20,
        help_text="Broker identifier, e.g. 'binance', 'bybit'",
    )
    label = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Human-readable label, e.g. 'Main Account', 'Testnet'",
    )
    api_key = models.BinaryField(
        help_text="Encrypted API key",
    )
    api_secret = models.BinaryField(
        help_text="Encrypted API secret",
    )
    is_testnet = models.BooleanField(
        default=False,
        help_text="Whether these credentials point to the testnet",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Disable without deleting",
    )
    last_validated = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last successful API test using these credentials",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "broker", "label")
        verbose_name = "Broker Credential"
        verbose_name_plural = "Broker Credentials"

    def __str__(self):
        label_suffix = f" — {self.label}" if self.label else ""
        return f"{self.user.username}@{self.broker}{label_suffix}"

    # ── Encryption helpers ──

    @staticmethod
    def _get_fernet() -> Fernet:
        """
        Derive a Fernet key from Django's SECRET_KEY using PBKDF2.
        This ensures a different key per project deployment.
        """
        import hashlib
        import base64

        # PBKDF2 with SECRET_KEY as password, 256-bit key
        key = hashlib.pbkdf2_hmac(
            "sha256",
            settings.SECRET_KEY.encode(),
            b"marketpulse_broker_credentials_v1",
            iterations=100_000,
            dklen=32,
        )
        return Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt a plaintext string and return bytes for BinaryField."""
        return self._get_fernet().encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt a BinaryField value back to plaintext string."""
        return self._get_fernet().decrypt(ciphertext).decode()

    def set_credentials(self, api_key: str, api_secret: str):
        """Encrypt and set both credentials."""
        self.api_key = self.encrypt(api_key)
        self.api_secret = self.encrypt(api_secret)

    def get_credentials(self) -> dict:
        """Decrypt and return both credentials as a dict."""
        return {
            "api_key": self.decrypt(bytes(self.api_key)),
            "api_secret": self.decrypt(bytes(self.api_secret)),
        }

    def clean(self):
        """Validate broker name against registered brokers."""
        from apps.common.broker_config import BROKER_CONFIGS

        if self.broker.lower() not in BROKER_CONFIGS:
            available = ", ".join(BROKER_CONFIGS.keys())
            raise ValidationError(
                {
                    "broker": f"Unknown broker '{self.broker}'. "
                    f"Available: {available}"
                }
            )
