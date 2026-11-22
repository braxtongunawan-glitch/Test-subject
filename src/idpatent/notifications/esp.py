"""E-mail provider abstraction (SRS 10.5).

SPF, DKIM and DMARC are configured on the sending domain and the ESP account;
this module enforces what can be checked per message: RFC 8058 one-click
List-Unsubscribe headers, HTTPS-only links and no executable attachments.
"""

from __future__ import annotations

import smtplib
import ssl
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Protocol

BLOCKED_ATTACHMENT_EXTENSIONS = frozenset(
    {".exe", ".bat", ".cmd", ".com", ".scr", ".js", ".jse", ".vbs", ".vbe", ".jar", ".msi", ".ps1", ".dll",
     ".hta", ".lnk", ".docm", ".xlsm", ".pptm", ".iso", ".apk"}
)


@dataclass
class OutboundEmail:
    to: str
    from_addr: str
    from_name: str
    subject: str
    text: str
    html: str
    unsubscribe_url: str
    unsubscribe_mailto: str | None = None
    reply_to: str | None = None
    message_id: str = field(default_factory=lambda: make_msgid(domain="idpatent.local"))
    in_reply_to: str | None = None
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)

    def headers(self) -> dict[str, str]:
        targets = [f"<{self.unsubscribe_url}>"]
        if self.unsubscribe_mailto:
            targets.append(f"<mailto:{self.unsubscribe_mailto}>")
        headers = {
            "List-Unsubscribe": ", ".join(targets),
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "Message-ID": self.message_id,
        }
        if self.in_reply_to:
            headers["In-Reply-To"] = self.in_reply_to
            headers["References"] = self.in_reply_to
        return headers


def validate_outbound(msg: OutboundEmail) -> list[str]:
    errors = []
    if not msg.unsubscribe_url.startswith("https://"):
        errors.append("UNSUBSCRIBE_URL_NOT_HTTPS")
    for name, _data, _ctype in msg.attachments:
        lower = name.lower()
        if any(lower.endswith(ext) for ext in BLOCKED_ATTACHMENT_EXTENSIONS):
            errors.append("EXECUTABLE_ATTACHMENT")
    if "\n" in msg.subject or "\r" in msg.subject:
        errors.append("HEADER_INJECTION")
    return errors


class EmailProvider(Protocol):
    name: str

    def send(self, msg: OutboundEmail) -> str: ...


class OutboxProvider:
    """Captures messages instead of sending them (development, tests, dry runs)."""

    name = "outbox"

    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []

    def send(self, msg: OutboundEmail) -> str:
        errors = validate_outbound(msg)
        if errors:
            raise ValueError(", ".join(errors))
        self.sent.append(msg)
        return f"outbox-{uuid.uuid4()}"


class SmtpProvider:
    """SMTP submission to an ESP relay; TLS is mandatory (STARTTLS or implicit TLS)."""

    name = "smtp"

    def __init__(self, host: str, port: int, username: str | None, password: str | None, implicit_tls: bool = False):
        self.host, self.port, self.username, self.password, self.implicit_tls = host, port, username, password, implicit_tls

    def build(self, msg: OutboundEmail) -> EmailMessage:
        em = EmailMessage()
        em["From"] = formataddr((msg.from_name, msg.from_addr))
        em["To"] = msg.to
        em["Subject"] = msg.subject
        if msg.reply_to:
            em["Reply-To"] = msg.reply_to
        for k, v in msg.headers().items():
            em[k] = v
        em.set_content(msg.text)
        em.add_alternative(msg.html, subtype="html")
        for name, data, ctype in msg.attachments:
            maintype, _, subtype = ctype.partition("/")
            em.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
        return em

    def send(self, msg: OutboundEmail) -> str:
        errors = validate_outbound(msg)
        if errors:
            raise ValueError(", ".join(errors))
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        em = self.build(msg)
        if self.implicit_tls:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as s:
                self._deliver(s, em)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.starttls(context=context)
                self._deliver(s, em)
        return msg.message_id

    def _deliver(self, smtp: smtplib.SMTP, em: EmailMessage) -> None:
        if self.username:
            smtp.login(self.username, self.password or "")
        smtp.send_message(em)
