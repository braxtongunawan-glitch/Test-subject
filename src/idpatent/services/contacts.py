"""Contacts, consent ledger, suppression list, opt-out and data-subject requests (UU PDP)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..compliance.gate import ContactCompliance, is_personal_email
from ..db.models import (
    Communication, ConsentEvent, Contact, LegitimateInterestAssessment, Owner, SuppressionEntry, utcnow,
)
from ..domain.dates import format_datetime_wib
from ..domain.enums import LawfulBasis, Purpose, Relationship
from ..domain.i18n import label
from ..security.crypto import mask_email
from . import audit
from .audit import Actor
from .context import AppContext

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ContactInput:
    email: str
    name: str | None = None
    phone: str | None = None
    owner_id: int | None = None
    role_title: str | None = None
    language: str = "id"
    relationship: Relationship = Relationship.CLIENT
    lawful_basis: LawfulBasis | None = None
    lia_id: int | None = None
    basis_note: str | None = None


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 320:
        raise ContactError("INVALID_EMAIL", "Alamat email tidak valid")
    return email


def create_contact(session: Session, ctx: AppContext, firm_id: int, data: ContactInput, actor: Actor) -> Contact:
    email = normalize_email(data.email)
    index = ctx.cipher.blind_index(email)
    if session.execute(select(Contact.id).where(Contact.firm_id == firm_id, Contact.email_index == index,
                                                 Contact.erased_at.is_(None))).first():
        raise ContactError("DUPLICATE_CONTACT", "Kontak dengan email ini sudah ada")
    if data.owner_id is not None:
        owner = session.get(Owner, data.owner_id)
        if owner is None or owner.firm_id != firm_id:
            raise ContactError("OWNER_NOT_FOUND", "Pemegang paten tidak ditemukan")
    if data.lia_id is not None:
        lia = session.get(LegitimateInterestAssessment, data.lia_id)
        if lia is None or lia.firm_id != firm_id:
            raise ContactError("LIA_NOT_FOUND", "LIA tidak ditemukan")
    if data.lawful_basis == LawfulBasis.LEGITIMATE_INTEREST and data.lia_id is None:
        raise ContactError("LIA_REQUIRED", "Dasar kepentingan yang sah memerlukan LIA yang terdokumentasi")
    if data.relationship == Relationship.PROSPECT:
        if not data.basis_note or len(data.basis_note.strip()) < 10:
            raise ContactError("BASIS_NOTE_REQUIRED", "Dokumentasikan sumber dan dasar pemrosesan kontak prospek")
        if data.lawful_basis not in (LawfulBasis.LEGITIMATE_INTEREST, LawfulBasis.CONSENT):
            raise ContactError("PROSPECT_BASIS", "Kontak prospek memerlukan dasar persetujuan atau kepentingan yang sah")
    contact = Contact(
        firm_id=firm_id,
        owner_id=data.owner_id,
        name_enc=ctx.cipher.encrypt(data.name, "contact.name") if data.name else None,
        email_enc=ctx.cipher.encrypt(email, "contact.email"),
        email_index=index,
        email_domain=email.split("@", 1)[1],
        phone_enc=ctx.cipher.encrypt(data.phone, "contact.phone") if data.phone else None,
        role_title=data.role_title,
        language=data.language if data.language in ("id", "en") else "id",
        relationship_type=data.relationship.value,
        lawful_basis=data.lawful_basis.value if data.lawful_basis else None,
        lia_id=data.lia_id,
        basis_note=data.basis_note,
        is_personal_email=is_personal_email(email),
        opted_out_purposes=[],
    )
    session.add(contact)
    session.flush()
    audit.record(session, actor, "contact.create", "contact", contact.id,
                 {"relationship": data.relationship.value, "lawful_basis": contact.lawful_basis,
                  "personal_email": contact.is_personal_email}, firm_id)
    return contact


def decrypt_email(ctx: AppContext, contact: Contact) -> str:
    return ctx.cipher.decrypt(contact.email_enc, "contact.email")


def contact_view(ctx: AppContext, contact: Contact, *, pii: bool, lang: str = "id") -> dict:
    email = decrypt_email(ctx, contact) if contact.erased_at is None else None
    name = ctx.cipher.decrypt(contact.name_enc, "contact.name") if contact.name_enc else None
    return {
        "id": contact.id,
        "owner_id": contact.owner_id,
        "name": name if pii else (name[:1] + "***" if name else None),
        "email": (email if pii else mask_email(email)) if email else None,
        "phone": (ctx.cipher.decrypt(contact.phone_enc, "contact.phone") if pii and contact.phone_enc else None),
        "role_title": contact.role_title,
        "language": contact.language,
        "relationship": contact.relationship_type,
        "lawful_basis": contact.lawful_basis,
        "lawful_basis_label": label(LawfulBasis(contact.lawful_basis), lang) if contact.lawful_basis else None,
        "lia_id": contact.lia_id,
        "is_personal_email": contact.is_personal_email,
        "opted_out_all": contact.opted_out_all,
        "opted_out_purposes": contact.opted_out_purposes,
        "erased": contact.erased_at is not None,
    }


def latest_consents(session: Session, contact_id: int) -> dict[Purpose, bool]:
    result: dict[Purpose, bool] = {}
    rows = session.execute(
        select(ConsentEvent).where(ConsentEvent.contact_id == contact_id).order_by(ConsentEvent.id)
    ).scalars()
    for ev in rows:
        result[Purpose(ev.purpose)] = ev.granted
    return result


def is_suppressed(session: Session, firm_id: int, email_index: str, domain: str) -> bool:
    q = select(SuppressionEntry.id).where(
        or_(SuppressionEntry.firm_id.is_(None), SuppressionEntry.firm_id == firm_id),
        or_(SuppressionEntry.email_index == email_index, SuppressionEntry.domain == domain.lower()),
    )
    return session.execute(q).first() is not None


def compliance_profile(session: Session, contact: Contact) -> ContactCompliance:
    lia = contact.lia
    return ContactCompliance(
        relationship=Relationship(contact.relationship_type),
        email_domain_personal=contact.is_personal_email,
        suppressed=contact.erased_at is not None
        or is_suppressed(session, contact.firm_id, contact.email_index, contact.email_domain),
        opted_out_purposes=frozenset(Purpose(p) for p in contact.opted_out_purposes or []),
        opted_out_all=contact.opted_out_all,
        consent=latest_consents(session, contact.id),
        lawful_basis=LawfulBasis(contact.lawful_basis) if contact.lawful_basis else None,
        lia_approved=bool(lia and lia.approved_at),
        lia_review_due=lia.review_due if lia else None,
    )


def record_consent(session: Session, contact: Contact, purpose: Purpose, granted: bool, source: str, actor: Actor,
                   evidence: str | None = None) -> ConsentEvent:
    ev = ConsentEvent(contact_id=contact.id, purpose=purpose.value, granted=granted, source=source,
                      evidence=evidence, ip=actor.ip, recorded_by=actor.user_id)
    session.add(ev)
    purposes = set(contact.opted_out_purposes or [])
    if granted:
        purposes.discard(purpose.value)
    else:
        purposes.add(purpose.value)
    contact.opted_out_purposes = sorted(purposes)
    session.flush()
    audit.record(session, actor, "consent.grant" if granted else "consent.withdraw", "contact", contact.id,
                 {"purpose": purpose.value, "source": source}, contact.firm_id)
    return ev


def opt_out(session: Session, ctx: AppContext, contact: Contact, purpose: Purpose | None, source: str, actor: Actor) -> None:
    """Honour an opt-out immediately. ``purpose=None`` means all e-mails from this firm."""
    if purpose is None:
        contact.opted_out_all = True
        for p in Purpose:
            session.add(ConsentEvent(contact_id=contact.id, purpose=p.value, granted=False, source=source,
                                     ip=actor.ip, recorded_by=actor.user_id))
        contact.opted_out_purposes = sorted(p.value for p in Purpose)
        add_suppression(session, contact.firm_id, contact.email_index, None, "OPT_OUT", actor)
        audit.record(session, actor, "contact.opt_out_all", "contact", contact.id, {"source": source}, contact.firm_id)
    else:
        record_consent(session, contact, purpose, False, source, actor)
    session.flush()


def add_suppression(session: Session, firm_id: int | None, email_index: str | None, domain: str | None, reason: str,
                    actor: Actor) -> SuppressionEntry:
    if not email_index and not domain:
        raise ContactError("SUPPRESSION_TARGET_REQUIRED", "Email atau domain wajib diisi")
    q = select(SuppressionEntry).where(
        SuppressionEntry.firm_id.is_(None) if firm_id is None else SuppressionEntry.firm_id == firm_id,
        SuppressionEntry.email_index == email_index if email_index else SuppressionEntry.domain == domain,
    )
    existing = session.execute(q).scalar()
    if existing:
        return existing
    entry = SuppressionEntry(firm_id=firm_id, email_index=email_index, domain=domain.lower() if domain else None,
                             reason=reason, created_by=actor.user_id)
    session.add(entry)
    session.flush()
    audit.record(session, actor, "suppression.add", "suppression", entry.id,
                 {"reason": reason, "scope": "global" if firm_id is None else "firm", "domain": entry.domain}, firm_id)
    return entry


def dsar_export(session: Session, ctx: AppContext, contact: Contact, actor: Actor) -> dict:
    """Everything held about a contact: personal data, consents, communications (UU PDP access right)."""
    consents = session.execute(
        select(ConsentEvent).where(ConsentEvent.contact_id == contact.id).order_by(ConsentEvent.id)
    ).scalars()
    comms = session.execute(
        select(Communication).where(Communication.contact_id == contact.id).order_by(Communication.id)
    ).scalars()
    data = {
        "contact": contact_view(ctx, contact, pii=True),
        "basis_note": contact.basis_note,
        "consent_history": [
            {"purpose": c.purpose, "granted": c.granted, "source": c.source,
             "captured_at": format_datetime_wib(c.captured_at), "evidence": c.evidence}
            for c in consents
        ],
        "communications": [
            {"id": c.id, "purpose": c.purpose, "subject": c.subject, "status": c.status,
             "lawful_basis": c.lawful_basis, "sent_at": format_datetime_wib(c.sent_at) if c.sent_at else None}
            for c in comms
        ],
        "suppressed": is_suppressed(session, contact.firm_id, contact.email_index, contact.email_domain),
        "exported_at": format_datetime_wib(utcnow()),
    }
    audit.record(session, actor, "contact.dsar_export", "contact", contact.id, {}, contact.firm_id)
    return data


def erase_contact(session: Session, ctx: AppContext, contact: Contact, actor: Actor) -> None:
    """Erase personal data but keep a keyed hash on the suppression list so the person is never re-contacted."""
    add_suppression(session, contact.firm_id, contact.email_index, None, "ERASURE", actor)
    contact.name_enc = None
    contact.phone_enc = None
    contact.email_enc = ctx.cipher.encrypt("", "contact.email")
    contact.role_title = None
    contact.basis_note = None
    contact.erased_at = utcnow()
    contact.opted_out_all = True
    session.flush()
    audit.record(session, actor, "contact.erase", "contact", contact.id, {}, contact.firm_id)


def lia_active(lia: LegitimateInterestAssessment, today: date) -> bool:
    return bool(lia.approved_at) and (lia.review_due is None or lia.review_due >= today)
