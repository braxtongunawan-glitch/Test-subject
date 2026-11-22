"""Application context: wires settings to the long-lived collaborators."""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Callable
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from ..config import Settings
from ..db.session import create_schema, make_engine, make_sessionmaker
from ..domain.dates import today_wib
from ..domain.rules import AlertPolicy, RuleRegistry, load_default_alert_policy, load_default_registry
from ..notifications.esp import EmailProvider, OutboxProvider, SmtpProvider
from ..notifications.renderer import TemplateRenderer
from ..security.crypto import FieldCipher, StaticKeyProvider

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    sessions: sessionmaker
    registry: RuleRegistry
    default_alert_policy: AlertPolicy
    cipher: FieldCipher
    token_key: bytes
    renderer: TemplateRenderer
    email: EmailProvider
    today: Callable[[], date] = field(default=today_wib)  # injectable clock (WIB calendar date)


def build_context(settings: Settings, *, email: EmailProvider | None = None, create_tables: bool = True,
                  today: Callable[[], date] | None = None) -> AppContext:
    engine = make_engine(settings.database_url)
    if create_tables:
        create_schema(engine)
    if settings.data_key_b64 and settings.index_key_b64:
        keys = StaticKeyProvider.from_b64(settings.data_key_b64, settings.index_key_b64)
    else:
        log.warning("No encryption keys configured: using ephemeral keys (development only, data unreadable after restart)")
        keys = StaticKeyProvider.ephemeral()
    token_key = base64.b64decode(settings.token_key_b64) if settings.token_key_b64 else os.urandom(32)
    registry = (
        RuleRegistry.from_directory(Path(settings.rules_directory)) if settings.rules_directory else load_default_registry()
    )
    if email is None:
        if settings.email_provider == "smtp":
            if not settings.smtp_host:
                raise ValueError("IDPATENT_SMTP_HOST is required for the smtp provider")
            email = SmtpProvider(settings.smtp_host, settings.smtp_port, settings.smtp_username,
                                 settings.smtp_password, settings.smtp_implicit_tls)
        else:
            email = OutboxProvider()
    return AppContext(
        settings=settings,
        sessions=make_sessionmaker(engine),
        registry=registry,
        default_alert_policy=load_default_alert_policy(),
        cipher=FieldCipher(keys),
        token_key=token_key,
        renderer=TemplateRenderer(),
        email=email,
        today=today or today_wib,
    )
