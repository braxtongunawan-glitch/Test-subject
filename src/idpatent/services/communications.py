"""Campaigns, review queue, compliant sending and delivery-event tracking (SRS 3.4, 5)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..compliance import tokens
from ..compliance.gate import SendContext, evaluate_send, requires_approval
from ..db.models import (
    Campaign, Communication, CommunicationEvent, Contact, Firm, Patent, TemplateApproval, utcnow,
)
from ..domain.dates import WIB, format_date
from ..domain.enums import (
    INACTIVE_STATUSES, DataSource, LawfulBasis, LegalStatus, Purpose, Relationship,
)
from ..domain.i18n import label
from ..notifications.esp import OutboundEmail
from ..notifications.renderer import TEMPLATE_KEYS, FooterLinks, RenderedEmail, SenderIdentity
from . import audit
from .audit import Actor
from .contacts import add_suppression, compliance_profile, decrypt_email
from .context import AppContext
from .patents import compute_for
from .views import email_patent_context


class CommunicationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Recipient:
    contact_id: int
    patent_ids: list[int]


def sender_identity(firm: Firm) -> SenderIdentity:
    return SenderIdentity(firm.legal_name, firm.address, firm.email, firm.phone, firm.website)


def is_template_approved(session: Session, ctx: AppContext, key: str, lang: str) -> bool:
    current = ctx.renderer.content_hash(key, lang)
    return session.execute(
        select(TemplateApproval.id).where(
            TemplateApproval.template_key == key, TemplateApproval.language == lang,
            TemplateApproval.content_hash == current,
        )
    ).first() is not None


def approve_template(session: Session, ctx: AppContext, key: str, lang: str, counsel_name: str, notes: str | None,
                     actor: Actor) -> TemplateApproval:
    if (key, lang) not in ctx.renderer.available():
        raise CommunicationError("TEMPLATE_NOT_FOUND", "Template tidak ditemukan")
    if not counsel_name.strip():
        raise CommunicationError("COUNSEL_REQUIRED", "Nama konsultan hukum yang meninjau wajib diisi")
    content_hash = ctx.renderer.content_hash(key, lang)
    existing = session.execute(select(TemplateApproval).where(
        TemplateApproval.template_key == key, TemplateApproval.language == lang,
        TemplateApproval.content_hash == content_hash)).scalar()
    if existing:
        return existing
    row = TemplateApproval(template_key=key, language=lang, content_hash=content_hash, counsel_name=counsel_name.strip(),
                           notes=notes, approved_by=actor.user_id)
    session.add(row)
    session.flush()
    audit.record(session, actor, "template.approve", "template", f"{key}.{lang}",
                 {"content_hash": content_hash, "counsel": counsel_name})
    return row


# ---------------------------------------------------------------- recipients
def suggest_recipients(session: Session, firm_id: int, purpose: Purpose, as_of: date, within_days: int = 90) -> list[Recipient]:
    """Group patents with obligations inside the window by owner, addressed to that owner's contacts."""
    until = as_of + timedelta(days=within_days)
    q = select(Patent).where(Patent.firm_id == firm_id, Patent.owner_id.is_not(None))
    if purpose == Purpose.ANNUITY_REMINDER:
        q = q.where(Patent.next_annuity_due.is_not(None), Patent.next_annuity_due <= until)
    elif purpose == Purpose.WORKING_STATEMENT_REMINDER:
        q = q.where(Patent.next_ws_due.is_not(None), Patent.next_ws_due <= until)
    elif purpose == Purpose.EXPIRY_NOTICE:
        q = q.where(Patent.protection_end >= as_of, Patent.protection_end <= until)
    else:
        raise CommunicationError("PURPOSE_NOT_SUGGESTIBLE", "Gunakan daftar penerima manual untuk tujuan ini")
    by_owner: dict[int, list[int]] = {}
    for p in session.execute(q).scalars():
        if p.computed_status and LegalStatus(p.computed_status) in INACTIVE_STATUSES:
            continue
        by_owner.setdefault(p.owner_id, []).append(p.id)
    recipients = []
    for owner_id, patent_ids in sorted(by_owner.items()):
        contacts = session.execute(
            select(Contact).where(Contact.firm_id == firm_id, Contact.owner_id == owner_id,
                                  Contact.erased_at.is_(None), Contact.opted_out_all.is_(False))
        ).scalars()
        for c in contacts:
            if purpose.value not in (c.opted_out_purposes or []):
                recipients.append(Recipient(c.id, sorted(patent_ids)))
    return recipients


# ---------------------------------------------------------------- campaigns
def create_campaign(session: Session, ctx: AppContext, firm: Firm, actor: Actor, name: str, purpose: Purpose,
                    recipients: list[Recipient]) -> Campaign:
    if not recipients:
        raise CommunicationError("NO_RECIPIENTS", "Tidak ada penerima")
    contacts: dict[int, Contact] = {}
    for r in recipients:
        c = session.get(Contact, r.contact_id)
        if c is None or c.firm_id != firm.id:
            raise CommunicationError("CONTACT_NOT_FOUND", f"Kontak {r.contact_id} tidak ditemukan")
        if c.erased_at is not None:
            raise CommunicationError("CONTACT_ERASED", f"Kontak {r.contact_id} telah dihapus")
        for pid in r.patent_ids:
            p = session.get(Patent, pid)
            if p is None or p.firm_id != firm.id:
                raise CommunicationError("PATENT_NOT_FOUND", f"Paten {pid} tidak ditemukan")
            # Relevance: messages must concern the recipient's own patents (agents may cover several owners).
            if c.relationship_type != Relationship.AGENT.value and p.owner_id != c.owner_id:
                raise CommunicationError("PATENT_NOT_OWNED_BY_CONTACT_OWNER",
                                         f"Paten {p.display_number} bukan milik pemegang yang terkait dengan kontak {c.id}")
        contacts[c.id] = c
    is_cold = any(c.relationship_type == Relationship.PROSPECT.value for c in contacts.values())
    needs_approval = any(
        requires_approval(Relationship(c.relationship_type), len(recipients), firm.bulk_threshold) for c in contacts.values()
    )
    campaign = Campaign(firm_id=firm.id, name=name, purpose=purpose.value, is_cold=is_cold,
                        requires_approval=needs_approval, created_by=actor.user_id)
    session.add(campaign)
    session.flush()
    for r in recipients:
        c = contacts[r.contact_id]
        session.add(Communication(
            firm_id=firm.id, campaign_id=campaign.id, contact_id=c.id, patent_ids=list(r.patent_ids),
            purpose=purpose.value, language=c.language, template_key=TEMPLATE_KEYS[purpose],
            is_cold=c.relationship_type == Relationship.PROSPECT.value, created_by=actor.user_id,
        ))
    session.flush()
    audit.record(session, actor, "campaign.create", "campaign", campaign.id,
                 {"purpose": purpose.value, "recipients": len(recipients), "cold": is_cold,
                  "requires_approval": needs_approval}, firm.id)
    return campaign


def submit_campaign(session: Session, campaign: Campaign, actor: Actor) -> Campaign:
    if campaign.status != "DRAFT":
        raise CommunicationError("INVALID_STATE", "Kampanye tidak dalam status draf")
    campaign.submitted_at = utcnow()
    campaign.status = "PENDING_APPROVAL" if campaign.requires_approval else "APPROVED"
    audit.record(session, actor, "campaign.submit", "campaign", campaign.id, {"status": campaign.status}, campaign.firm_id)
    return campaign


def review_campaign(session: Session, campaign: Campaign, actor: Actor, approve: bool, note: str | None) -> Campaign:
    if campaign.status != "PENDING_APPROVAL":
        raise CommunicationError("INVALID_STATE", "Kampanye tidak menunggu persetujuan")
    if actor.user_id == campaign.created_by:
        raise CommunicationError("FOUR_EYES", "Pembuat kampanye tidak dapat menyetujui kampanyenya sendiri")
    if campaign.is_cold and approve and not (note and len(note.strip()) >= 10):
        raise CommunicationError("COLD_REVIEW_NOTE_REQUIRED", "Kampanye tanpa hubungan sebelumnya memerlukan catatan peninjauan")
    campaign.status = "APPROVED" if approve else "REJECTED"
    campaign.approved_by = actor.user_id
    campaign.approved_at = utcnow()
    campaign.review_note = note
    audit.record(session, actor, "campaign.approve" if approve else "campaign.reject", "campaign", campaign.id,
                 {"note": note}, campaign.firm_id)
    return campaign


def _links(ctx: AppContext, comm: Communication) -> FooterLinks:
    base = ctx.settings.public_base_url
    unsub = tokens.sign({"c": comm.contact_id, "f": comm.firm_id, "p": comm.purpose, "m": comm.id}, ctx.token_key)
    prefs = tokens.sign({"c": comm.contact_id, "f": comm.firm_id, "k": "prefs"}, ctx.token_key, ttl_seconds=90 * 86400)
    return FooterLinks(f"{base}/berhenti-langganan/{unsub}", f"{base}/preferensi/{prefs}", ctx.settings.resolved_privacy_url)


def render_communication(session: Session, ctx: AppContext, firm: Firm, comm: Communication, as_of: date,
                         lawful_basis: LawfulBasis | None) -> RenderedEmail:
    contact = comm.contact
    lang = comm.language
    patents = [session.get(Patent, pid) for pid in comm.patent_ids]
    patent_ctx = []
    sources = set()
    for p in patents:
        comp = compute_for(ctx, p, as_of)
        patent_ctx.append(email_patent_context(p, comp, lang))
        sources.add(label(DataSource(p.source), lang))
    owner_name = contact.owner.name if contact.owner else (patents[0].owner.name if patents and patents[0].owner else "-")
    name = ctx.cipher.decrypt(contact.name_enc, "contact.name") if contact.name_enc else None
    context = {
        "recipient_name": name,
        "owner_name": owner_name,
        "patents": patent_ctx,
        "services": firm.services_id if lang == "id" else firm.services_en,
        "data_source_text": ", ".join(sorted(sources)) or "-",
        "as_of_text": format_date(as_of, lang),
    }
    return ctx.renderer.render(
        comm.template_key, lang, context, sender=sender_identity(firm), links=_links(ctx, comm),
        purpose=Purpose(comm.purpose), lawful_basis=lawful_basis, is_cold=comm.is_cold,
    )


def _cold_sent_today(session: Session, firm_id: int, now: datetime) -> int:
    start_wib = now.astimezone(WIB).replace(hour=0, minute=0, second=0, microsecond=0)
    return session.execute(
        select(func.count(Communication.id)).where(
            Communication.firm_id == firm_id, Communication.is_cold.is_(True), Communication.status != "QUEUED",
            Communication.status != "BLOCKED", Communication.sent_at >= start_wib,
        )
    ).scalar_one()


def send_campaign(session: Session, ctx: AppContext, firm: Firm, campaign: Campaign, actor: Actor, as_of: date) -> dict:
    if campaign.status not in ("APPROVED", "PARTIAL"):
        raise CommunicationError("NOT_APPROVED", "Kampanye belum disetujui")
    sent = blocked = 0
    queued = [c for c in campaign.communications if c.status == "QUEUED"]
    for comm in queued:
        result = send_one(session, ctx, firm, comm, actor, as_of, campaign_approved=True, recipient_count=len(campaign.communications))
        if result == "SENT":
            sent += 1
        else:
            blocked += 1
    campaign.status = "SENT" if blocked == 0 else "PARTIAL"
    audit.record(session, actor, "campaign.send", "campaign", campaign.id, {"sent": sent, "blocked": blocked}, firm.id)
    return {"sent": sent, "blocked": blocked, "status": campaign.status}


def send_one(session: Session, ctx: AppContext, firm: Firm, comm: Communication, actor: Actor, as_of: date, *,
             campaign_approved: bool, recipient_count: int) -> str:
    contact = comm.contact
    profile = compliance_profile(session, contact)
    now = utcnow()
    decision = evaluate_send(profile, SendContext(
        purpose=Purpose(comm.purpose),
        patent_count=len(comm.patent_ids),
        recipient_count=recipient_count,
        campaign_approved=campaign_approved,
        template_approved=is_template_approved(session, ctx, comm.template_key, comm.language),
        sender_identity_complete=sender_identity(firm).complete,
        production=ctx.settings.production,
        cold_sent_today=_cold_sent_today(session, firm.id, now) if comm.is_cold else 0,
        cold_daily_cap=firm.cold_daily_cap,
        bulk_threshold=firm.bulk_threshold,
        today=as_of,
    ))
    comm.warnings = decision.warnings
    if not decision.allowed:
        comm.status = "BLOCKED"
        comm.blocked_reasons = decision.blocked_reasons
        audit.record(session, actor, "communication.blocked", "communication", comm.id,
                     {"reasons": decision.blocked_reasons}, firm.id)
        return "BLOCKED"
    rendered = render_communication(session, ctx, firm, comm, as_of, decision.lawful_basis)
    links = _links(ctx, comm)
    msg = OutboundEmail(
        to=decrypt_email(ctx, contact), from_addr=firm.email, from_name=firm.legal_name, subject=rendered.subject,
        text=rendered.text, html=rendered.html, unsubscribe_url=links.unsubscribe_url,
        unsubscribe_mailto=ctx.settings.unsubscribe_mailto, reply_to=firm.email,
    )
    try:
        provider_id = ctx.email.send(msg)
    except Exception as exc:  # provider failure: keep queued for retry, record the error
        comm.blocked_reasons = [f"PROVIDER_ERROR: {type(exc).__name__}"]
        audit.record(session, actor, "communication.provider_error", "communication", comm.id,
                     {"error": type(exc).__name__}, firm.id)
        return "ERROR"
    comm.status = "SENT"
    comm.blocked_reasons = []
    comm.subject = rendered.subject
    comm.body_sha256 = hashlib.sha256(rendered.text.encode()).hexdigest()
    comm.template_hash = rendered.template_hash
    comm.lawful_basis = decision.lawful_basis.value if decision.lawful_basis else None
    comm.message_id = msg.message_id
    comm.thread_id = comm.thread_id or msg.message_id
    comm.provider = getattr(ctx.email, "name", "unknown")
    comm.provider_message_id = provider_id
    comm.sent_at = now
    audit.record(session, actor, "communication.sent", "communication", comm.id,
                 {"purpose": comm.purpose, "lawful_basis": comm.lawful_basis, "cold": comm.is_cold,
                  "template_hash": rendered.template_hash, "patents": comm.patent_ids}, firm.id)
    return "SENT"


EVENT_STATUS = {
    "delivered": "DELIVERED", "opened": "OPENED", "replied": "REPLIED", "bounced": "BOUNCED",
    "complained": "COMPLAINED", "unsubscribed": "OPTED_OUT",
}
_STATUS_ORDER = ["QUEUED", "SENT", "DELIVERED", "OPENED", "REPLIED"]


def handle_esp_event(session: Session, ctx: AppContext, payload: dict) -> Communication | None:
    event = str(payload.get("event", "")).lower()
    if event not in EVENT_STATUS:
        raise CommunicationError("UNKNOWN_EVENT", "Jenis event tidak dikenal")
    key = payload.get("message_id") or payload.get("in_reply_to")
    provider_id = payload.get("provider_message_id")
    comm = None
    if key:
        comm = session.execute(select(Communication).where(Communication.message_id == key)).scalar()
    if comm is None and provider_id:
        comm = session.execute(select(Communication).where(Communication.provider_message_id == provider_id)).scalar()
    if comm is None:
        return None
    actor = Actor.system("esp-webhook", comm.firm_id)
    session.add(CommunicationEvent(communication_id=comm.id, event=EVENT_STATUS[event],
                                   payload={k: v for k, v in payload.items() if k in ("event", "bounce_type", "occurred_at")}))
    new_status = EVENT_STATUS[event]
    now = utcnow()
    if new_status == "OPENED":
        comm.opened_at = comm.opened_at or now
    if new_status == "REPLIED":
        comm.replied_at = comm.replied_at or now
    if new_status in _STATUS_ORDER and comm.status in _STATUS_ORDER:
        if _STATUS_ORDER.index(new_status) > _STATUS_ORDER.index(comm.status):
            comm.status = new_status
    else:
        comm.status = new_status
    contact = comm.contact
    if new_status == "BOUNCED" and str(payload.get("bounce_type", "hard")).lower() == "hard":
        add_suppression(session, comm.firm_id, contact.email_index, None, "HARD_BOUNCE", actor)
    elif new_status == "COMPLAINED":
        add_suppression(session, None, contact.email_index, None, "COMPLAINT", actor)
    elif new_status == "OPTED_OUT":
        from .contacts import opt_out

        opt_out(session, ctx, contact, None, "ESP_UNSUBSCRIBE", actor)
    audit.record(session, actor, f"communication.{event}", "communication", comm.id, {}, comm.firm_id)
    return comm


def communication_view(comm: Communication, lang: str = "id") -> dict:
    return {
        "id": comm.id,
        "campaign_id": comm.campaign_id,
        "contact_id": comm.contact_id,
        "patent_ids": comm.patent_ids,
        "purpose": comm.purpose,
        "purpose_label": label(Purpose(comm.purpose), lang),
        "language": comm.language,
        "status": comm.status,
        "blocked_reasons": comm.blocked_reasons,
        "warnings": comm.warnings,
        "lawful_basis": comm.lawful_basis,
        "is_cold": comm.is_cold,
        "subject": comm.subject,
        "message_id": comm.message_id,
        "thread_id": comm.thread_id,
        "sent_at": comm.sent_at.isoformat() if comm.sent_at else None,
        "opened_at": comm.opened_at.isoformat() if comm.opened_at else None,
        "replied_at": comm.replied_at.isoformat() if comm.replied_at else None,
    }


def campaign_view(c: Campaign, lang: str = "id") -> dict:
    counts: dict[str, int] = {}
    for comm in c.communications:
        counts[comm.status] = counts.get(comm.status, 0) + 1
    return {
        "id": c.id,
        "name": c.name,
        "purpose": c.purpose,
        "purpose_label": label(Purpose(c.purpose), lang),
        "status": c.status,
        "is_cold": c.is_cold,
        "requires_approval": c.requires_approval,
        "created_by": c.created_by,
        "approved_by": c.approved_by,
        "review_note": c.review_note,
        "recipients": len(c.communications),
        "status_counts": counts,
    }
