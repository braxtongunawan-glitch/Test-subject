"""Template rendering with an enforced compliance footer (SRS 3.4, 5, 10.5).

Template authors write only the subject and body. The renderer always appends
the footer – sender legal name and physical address, purpose, lawful basis,
privacy notice and a one-click opt-out link – so no template can omit them.
The rendered result is then validated (HTTPS-only links, footer present,
no unrendered placeholders).

Template files: ``templates/<key>.<lang>.j2``; the first line is the subject,
followed by a ``---`` line and the plain-text body.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from importlib import resources

from jinja2 import BaseLoader, Environment, StrictUndefined, TemplateNotFound

from ..domain.enums import LawfulBasis, Purpose
from ..domain.i18n import label

TEMPLATE_KEYS = {
    Purpose.ANNUITY_REMINDER: "annuity_reminder",
    Purpose.WORKING_STATEMENT_REMINDER: "working_statement_reminder",
    Purpose.EXPIRY_NOTICE: "expiry_notice",
    Purpose.STATUS_NOTICE: "status_notice",
    Purpose.COMMERCIAL_OFFER: "service_offer",
}

_URL = re.compile(r"\b(?:https?)://[^\s<>\"')]+", re.IGNORECASE)


class TemplateValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__(", ".join(errors))
        self.errors = errors


class _PackageLoader(BaseLoader):
    def get_source(self, environment, template):
        path = resources.files("idpatent.notifications.templates").joinpath(template)
        if not path.is_file():
            raise TemplateNotFound(template)
        source = path.read_text(encoding="utf-8")
        return source, None, lambda: True


@dataclass(frozen=True)
class SenderIdentity:
    legal_name: str
    address: str
    email: str
    phone: str | None = None
    website: str | None = None

    @property
    def complete(self) -> bool:
        return bool(self.legal_name.strip() and self.address.strip() and "@" in self.email)


@dataclass(frozen=True)
class FooterLinks:
    unsubscribe_url: str
    preferences_url: str
    privacy_url: str


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str
    template_key: str
    language: str
    template_hash: str


class TemplateRenderer:
    def __init__(self) -> None:
        self.env = Environment(
            loader=_PackageLoader(), undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True,
            keep_trailing_newline=False, autoescape=False,
        )

    @staticmethod
    def filename(key: str, lang: str) -> str:
        return f"{key}.{lang}.j2"

    def source(self, key: str, lang: str) -> str:
        return self.env.loader.get_source(self.env, self.filename(key, lang))[0]

    def content_hash(self, key: str, lang: str) -> str:
        return hashlib.sha256(self.source(key, lang).encode("utf-8")).hexdigest()

    def available(self) -> list[tuple[str, str]]:
        out = []
        for entry in resources.files("idpatent.notifications.templates").iterdir():
            parts = entry.name.split(".")
            if len(parts) == 3 and parts[2] == "j2":
                out.append((parts[0], parts[1]))
        return sorted(out)

    def render(
        self,
        key: str,
        lang: str,
        context: dict,
        *,
        sender: SenderIdentity,
        links: FooterLinks,
        purpose: Purpose,
        lawful_basis: LawfulBasis | None,
        is_cold: bool,
    ) -> RenderedEmail:
        template = self.env.get_template(self.filename(key, lang))
        rendered = template.render(**context, sender=sender)
        if "\n---\n" not in rendered:
            raise TemplateValidationError(["TEMPLATE_MISSING_SUBJECT_SEPARATOR"])
        subject, body = rendered.split("\n---\n", 1)
        subject = " ".join(subject.split())
        body = re.sub(r"\n{3,}", "\n\n", body.strip())
        footer = _footer(lang, sender, links, purpose, lawful_basis, is_cold, context.get("owner_name", ""))
        text = f"{body}\n\n{footer}\n"
        errors = validate_rendered(subject, text, links)
        if errors:
            raise TemplateValidationError(errors)
        return RenderedEmail(subject, text, text_to_html(text, lang), key, lang, self.content_hash(key, lang))


def _footer(
    lang: str,
    sender: SenderIdentity,
    links: FooterLinks,
    purpose: Purpose,
    basis: LawfulBasis | None,
    is_cold: bool,
    owner_name: str,
) -> str:
    contact = sender.email + (f", {sender.phone}" if sender.phone else "")
    basis_text = label(basis, lang) if basis else "-"
    if lang == "id":
        lines = [
            "--",
            f"Email ini dikirim oleh {sender.legal_name}, {sender.address}. Kontak: {contact}.",
            f"Tujuan pesan: {label(purpose, 'id')}. Dasar pemrosesan data pribadi: {basis_text}.",
        ]
        if is_cold:
            lines.append(
                f"Kami menghubungi Anda terkait paten yang tercatat atas nama {owner_name} pada basis data publik "
                "PDKI (DJKI). Kami tidak akan menghubungi Anda kembali apabila Anda tidak menghendakinya."
            )
        lines += [
            f"Kebijakan Privasi: {links.privacy_url}",
            f"Atur preferensi email: {links.preferences_url}",
            f"Berhenti menerima email seperti ini (satu klik): {links.unsubscribe_url}",
        ]
    else:
        lines = [
            "--",
            f"This e-mail was sent by {sender.legal_name}, {sender.address}. Contact: {contact}.",
            f"Purpose: {label(purpose, 'en')}. Legal basis for processing personal data: {basis_text}.",
        ]
        if is_cold:
            lines.append(
                f"We are contacting you about patents recorded in the name of {owner_name} in the public PDKI "
                "database of the Indonesian DGIP. We will not contact you again if you prefer not to hear from us."
            )
        lines += [
            f"Privacy Policy: {links.privacy_url}",
            f"E-mail preferences: {links.preferences_url}",
            f"Unsubscribe from e-mails like this (one click): {links.unsubscribe_url}",
        ]
    return "\n".join(lines)


def validate_rendered(subject: str, text: str, links: FooterLinks) -> list[str]:
    errors = []
    if not subject or len(subject) > 200:
        errors.append("SUBJECT_INVALID")
    if links.unsubscribe_url not in text:
        errors.append("UNSUBSCRIBE_LINK_MISSING")
    for url in _URL.findall(text):
        if not url.lower().startswith("https://"):
            errors.append("NON_HTTPS_LINK")
            break
    for url in (links.unsubscribe_url, links.preferences_url, links.privacy_url):
        if not url.startswith("https://"):
            errors.append("NON_HTTPS_LINK")
            break
    if "{{" in text or "{%" in text:
        errors.append("UNRENDERED_PLACEHOLDER")
    return errors


def text_to_html(text: str, lang: str) -> str:
    def linkify(escaped: str) -> str:
        return _URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)

    paragraphs = [p for p in text.strip().split("\n\n") if p.strip()]
    body = "\n".join(f"<p>{linkify(html.escape(p)).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"></head>'
        f'<body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222">{body}</body></html>'
    )
