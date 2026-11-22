"""Runtime settings, read from environment variables (``IDPATENT_*``)."""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    environment: str = "development"  # development | test | production
    database_url: str = "sqlite:///./idpatent.db"
    data_key_b64: str | None = None  # AES-256 key for field encryption
    index_key_b64: str | None = None  # HMAC key for blind indexes
    token_key_b64: str | None = None  # HMAC key for opt-out / preference links
    public_base_url: str = "https://localhost:8000"
    privacy_url: str | None = None
    issuer_name: str = "IDPatent"
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    mfa_challenge_minutes: int = 5
    lockout_threshold: int = 5
    lockout_minutes: int = 15
    email_provider: str = "outbox"  # outbox | smtp
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_implicit_tls: bool = False
    unsubscribe_mailto: str | None = None
    esp_webhook_secret: str | None = None
    rules_directory: str | None = None  # override the packaged rule sets
    cookie_secure: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def production(self) -> bool:
        return self.environment == "production"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        g = lambda k, d=None: env.get(f"IDPATENT_{k}", d)  # noqa: E731
        s = cls(
            environment=g("ENV", "development"),
            database_url=g("DATABASE_URL", "sqlite:///./idpatent.db"),
            data_key_b64=g("DATA_KEY"),
            index_key_b64=g("INDEX_KEY"),
            token_key_b64=g("TOKEN_KEY"),
            public_base_url=g("PUBLIC_BASE_URL", "https://localhost:8000").rstrip("/"),
            privacy_url=g("PRIVACY_URL"),
            issuer_name=g("ISSUER_NAME", "IDPatent"),
            session_idle_minutes=int(g("SESSION_IDLE_MINUTES", 30)),
            session_absolute_hours=int(g("SESSION_ABSOLUTE_HOURS", 12)),
            lockout_threshold=int(g("LOCKOUT_THRESHOLD", 5)),
            lockout_minutes=int(g("LOCKOUT_MINUTES", 15)),
            email_provider=g("EMAIL_PROVIDER", "outbox"),
            smtp_host=g("SMTP_HOST"),
            smtp_port=int(g("SMTP_PORT", 587)),
            smtp_username=g("SMTP_USERNAME"),
            smtp_password=g("SMTP_PASSWORD"),
            smtp_implicit_tls=g("SMTP_IMPLICIT_TLS", "false").lower() == "true",
            unsubscribe_mailto=g("UNSUBSCRIBE_MAILTO"),
            esp_webhook_secret=g("ESP_WEBHOOK_SECRET"),
            rules_directory=g("RULES_DIRECTORY"),
            cookie_secure=g("COOKIE_SECURE", "true").lower() != "false",
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.production:
            missing = [k for k in ("data_key_b64", "index_key_b64", "token_key_b64", "esp_webhook_secret") if not getattr(self, k)]
            if missing:
                raise ConfigError(f"Missing required production settings: {', '.join(missing)}")
            if not self.public_base_url.startswith("https://"):
                raise ConfigError("IDPATENT_PUBLIC_BASE_URL must use https in production")
            if self.email_provider == "outbox":
                log.warning("Production is configured with the outbox provider: e-mails will not be delivered")
        for name in ("data_key_b64", "index_key_b64", "token_key_b64"):
            value = getattr(self, name)
            if value and len(base64.b64decode(value)) != 32:
                raise ConfigError(f"{name} must be 32 bytes, base64-encoded")

    @property
    def resolved_privacy_url(self) -> str:
        return self.privacy_url or f"{self.public_base_url}/kebijakan-privasi"
