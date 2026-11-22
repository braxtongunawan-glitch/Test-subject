"""Contacts, consent, suppression, LIAs, templates, campaigns and communications."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import Campaign, Communication, Contact, LegitimateInterestAssessment, SuppressionEntry, utcnow
from ...domain.enums import LawfulBasis, Purpose, Relationship
from ...domain.i18n import label
from ...notifications.renderer import TEMPLATE_KEYS, TemplateValidationError
from ...security.rbac import Permission
from ...services import audit
from ...services import communications as comms
from ...services import contacts as contact_service
from ...services.context import AppContext
from ..deps import Principal, api_error, get_ctx, get_db, guarded

router = APIRouter(tags=["outreach"])


def _contact_or_404(db: Session, principal: Principal, contact_id: int) -> Contact:
    c = db.get(Contact, contact_id)
    if c is None or c.firm_id != principal.firm_id:
        raise api_error(404, "NOT_FOUND", "Kontak tidak ditemukan")
    return c


def _err(exc):
    return api_error(422, getattr(exc, "code", "INVALID"), str(exc))


# ------------------------------------------------------------------ contacts
class ContactIn(BaseModel):
    email: str = Field(max_length=320)
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    owner_id: int | None = None
    role_title: str | None = Field(default=None, max_length=200)
    language: str = Field(default="id", pattern="^(id|en)$")
    relationship: Relationship = Relationship.CLIENT
    lawful_basis: LawfulBasis | None = None
    lia_id: int | None = None
    basis_note: str | None = Field(default=None, max_length=4000)


@router.post("/contacts", status_code=201)
def create_contact(body: ContactIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                   principal: Principal = Depends(guarded(Permission.CONTACT_WRITE))):
    try:
        c = contact_service.create_contact(db, ctx, principal.firm_id, contact_service.ContactInput(**body.model_dump()),
                                           principal.actor)
    except contact_service.ContactError as exc:
        raise _err(exc) from exc
    db.commit()
    return contact_service.contact_view(ctx, c, pii=True, lang=principal.lang)


@router.get("/contacts")
def list_contacts(owner_id: int | None = None, limit: int = Query(default=100, ge=1, le=1000), offset: int = 0,
                  ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                  principal: Principal = Depends(guarded(Permission.CONTACT_READ))):
    stmt = select(Contact).where(Contact.firm_id == principal.firm_id)
    if owner_id:
        stmt = stmt.where(Contact.owner_id == owner_id)
    pii = principal.can(Permission.CONTACT_READ_PII)
    rows = list(db.execute(stmt.order_by(Contact.id).limit(limit).offset(offset)).scalars())
    if pii and rows:
        audit.record(db, principal.actor, "contact.list_pii", "contact", None, {"count": len(rows)}, principal.firm_id)
        db.commit()
    return [contact_service.contact_view(ctx, c, pii=pii, lang=principal.lang) for c in rows]


@router.get("/contacts/{contact_id}")
def get_contact(contact_id: int, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                principal: Principal = Depends(guarded(Permission.CONTACT_READ))):
    c = _contact_or_404(db, principal, contact_id)
    pii = principal.can(Permission.CONTACT_READ_PII)
    if pii:
        audit.record(db, principal.actor, "contact.view_pii", "contact", c.id, {}, principal.firm_id)
        db.commit()
    view = contact_service.contact_view(ctx, c, pii=pii, lang=principal.lang)
    view["consents"] = {p.value: v for p, v in contact_service.latest_consents(db, c.id).items()}
    return view


class ConsentIn(BaseModel):
    purpose: Purpose
    granted: bool
    evidence: str = Field(min_length=5, max_length=4000)  # how/when consent was given or withdrawn


@router.post("/contacts/{contact_id}/consent")
def record_consent(contact_id: int, body: ConsentIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                   principal: Principal = Depends(guarded(Permission.CONTACT_WRITE))):
    c = _contact_or_404(db, principal, contact_id)
    contact_service.record_consent(db, c, body.purpose, body.granted, "STAFF", principal.actor, body.evidence)
    db.commit()
    return contact_service.contact_view(ctx, c, pii=False, lang=principal.lang)


class OptOutIn(BaseModel):
    purpose: Purpose | None = None


@router.post("/contacts/{contact_id}/opt-out")
def staff_opt_out(contact_id: int, body: OptOutIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                  principal: Principal = Depends(guarded(Permission.CONTACT_WRITE))):
    c = _contact_or_404(db, principal, contact_id)
    contact_service.opt_out(db, ctx, c, body.purpose, "STAFF", principal.actor)
    db.commit()
    return contact_service.contact_view(ctx, c, pii=False, lang=principal.lang)


@router.get("/contacts/{contact_id}/dsar-export")
def dsar_export(contact_id: int, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                principal: Principal = Depends(guarded(Permission.COMPLIANCE_MANAGE))):
    c = _contact_or_404(db, principal, contact_id)
    data = contact_service.dsar_export(db, ctx, c, principal.actor)
    db.commit()
    return data


@router.post("/contacts/{contact_id}/erase")
def erase(contact_id: int, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
          principal: Principal = Depends(guarded(Permission.COMPLIANCE_MANAGE))):
    c = _contact_or_404(db, principal, contact_id)
    contact_service.erase_contact(db, ctx, c, principal.actor)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ suppression & LIA
class SuppressionIn(BaseModel):
    email: str | None = Field(default=None, max_length=320)
    domain: str | None = Field(default=None, max_length=255)
    reason: str = Field(default="MANUAL", pattern="^(MANUAL|OPT_OUT|HARD_BOUNCE|COMPLAINT|ERASURE)$")
    global_scope: bool = False


@router.post("/compliance/suppressions", status_code=201)
def add_suppression(body: SuppressionIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                    principal: Principal = Depends(guarded(Permission.COMPLIANCE_MANAGE))):
    if body.global_scope and not principal.can(Permission.PLATFORM_ADMIN):
        raise api_error(403, "FORBIDDEN", "Hanya admin platform yang dapat menambah supresi global")
    index = ctx.cipher.blind_index(contact_service.normalize_email(body.email)) if body.email else None
    try:
        entry = contact_service.add_suppression(db, None if body.global_scope else principal.firm_id, index,
                                                body.domain, body.reason, principal.actor)
    except contact_service.ContactError as exc:
        raise _err(exc) from exc
    db.commit()
    return {"id": entry.id, "reason": entry.reason, "domain": entry.domain, "global": entry.firm_id is None}


@router.get("/compliance/suppressions")
def list_suppressions(db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.COMPLIANCE_MANAGE))):
    rows = db.execute(select(SuppressionEntry).where(
        (SuppressionEntry.firm_id == principal.firm_id) | (SuppressionEntry.firm_id.is_(None)))
        .order_by(SuppressionEntry.id.desc()).limit(1000)).scalars()
    # e-mail addresses are only held as keyed hashes; domains are shown
    return [{"id": r.id, "reason": r.reason, "domain": r.domain, "global": r.firm_id is None,
             "created_at": r.created_at.isoformat()} for r in rows]


class LiaIn(BaseModel):
    title: str = Field(max_length=300)
    purpose: str = Field(min_length=20, max_length=8000)
    necessity: str = Field(min_length=20, max_length=8000)
    balancing: str = Field(min_length=20, max_length=8000)
    safeguards: str = Field(min_length=20, max_length=8000)


def _lia_view(l: LegitimateInterestAssessment) -> dict:
    return {"id": l.id, "title": l.title, "purpose": l.purpose, "necessity": l.necessity, "balancing": l.balancing,
            "safeguards": l.safeguards, "approved": l.approved_at is not None,
            "approved_at": l.approved_at.isoformat() if l.approved_at else None,
            "review_due": l.review_due.isoformat() if l.review_due else None}


@router.post("/compliance/lias", status_code=201)
def create_lia(body: LiaIn, db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.COMPLIANCE_MANAGE))):
    lia = LegitimateInterestAssessment(firm_id=principal.firm_id, created_by=principal.user.id, **body.model_dump())
    db.add(lia)
    db.flush()
    audit.record(db, principal.actor, "lia.create", "lia", lia.id, {"title": lia.title}, principal.firm_id)
    db.commit()
    return _lia_view(lia)


class LiaApproveIn(BaseModel):
    review_due: date


@router.post("/compliance/lias/{lia_id}/approve")
def approve_lia(lia_id: int, body: LiaApproveIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                principal: Principal = Depends(guarded(Permission.COMPLIANCE_MANAGE))):
    lia = db.get(LegitimateInterestAssessment, lia_id)
    if lia is None or lia.firm_id != principal.firm_id:
        raise api_error(404, "NOT_FOUND", "LIA tidak ditemukan")
    if lia.created_by == principal.user.id:
        raise api_error(422, "FOUR_EYES", "LIA harus disetujui oleh pengguna lain")
    if body.review_due <= ctx.today():
        raise api_error(422, "REVIEW_DATE", "Tanggal tinjauan ulang harus di masa depan")
    lia.approved_by, lia.approved_at, lia.review_due = principal.user.id, utcnow(), body.review_due
    audit.record(db, principal.actor, "lia.approve", "lia", lia.id, {"review_due": body.review_due.isoformat()},
                 principal.firm_id)
    db.commit()
    return _lia_view(lia)


@router.get("/compliance/lias")
def list_lias(db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.COMPLIANCE_MANAGE))):
    rows = db.execute(select(LegitimateInterestAssessment).where(
        LegitimateInterestAssessment.firm_id == principal.firm_id)).scalars()
    return [_lia_view(l) for l in rows]


# ------------------------------------------------------------------ templates
@router.get("/templates")
def list_templates(ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                   principal: Principal = Depends(guarded(Permission.COMM_DRAFT))):
    out = []
    for key, lang in ctx.renderer.available():
        out.append({"key": key, "language": lang, "content_hash": ctx.renderer.content_hash(key, lang),
                    "counsel_approved": comms.is_template_approved(db, ctx, key, lang),
                    "source": ctx.renderer.source(key, lang)})
    return out


class TemplateApproveIn(BaseModel):
    counsel_name: str = Field(min_length=3, max_length=300)
    notes: str | None = Field(default=None, max_length=4000)


@router.post("/templates/{key}/{lang}/approve")
def approve_template(key: str, lang: str, body: TemplateApproveIn, ctx: AppContext = Depends(get_ctx),
                     db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.TEMPLATE_APPROVE))):
    try:
        row = comms.approve_template(db, ctx, key, lang, body.counsel_name, body.notes, principal.actor)
    except comms.CommunicationError as exc:
        raise _err(exc) from exc
    db.commit()
    return {"key": key, "language": lang, "content_hash": row.content_hash, "counsel_name": row.counsel_name}


# ------------------------------------------------------------------ campaigns
class RecipientIn(BaseModel):
    contact_id: int
    patent_ids: list[int] = Field(default_factory=list, max_length=200)


class CampaignIn(BaseModel):
    name: str = Field(max_length=300)
    purpose: Purpose
    recipients: list[RecipientIn] | None = Field(default=None, max_length=5000)
    auto_within_days: int | None = Field(default=None, ge=1, le=730)


@router.post("/campaigns", status_code=201)
def create_campaign(body: CampaignIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                    principal: Principal = Depends(guarded(Permission.COMM_DRAFT))):
    try:
        if body.recipients is not None:
            recipients = [comms.Recipient(r.contact_id, r.patent_ids) for r in body.recipients]
        elif body.auto_within_days:
            recipients = comms.suggest_recipients(db, principal.firm_id, body.purpose, ctx.today(), body.auto_within_days)
        else:
            raise api_error(422, "RECIPIENTS_REQUIRED", "Isi daftar penerima atau auto_within_days")
        campaign = comms.create_campaign(db, ctx, principal.firm, principal.actor, body.name, body.purpose, recipients)
    except comms.CommunicationError as exc:
        raise _err(exc) from exc
    db.commit()
    return comms.campaign_view(campaign, principal.lang)


@router.get("/campaigns")
def list_campaigns(status: str | None = None, db: Session = Depends(get_db),
                   principal: Principal = Depends(guarded(Permission.COMM_DRAFT))):
    stmt = select(Campaign).where(Campaign.firm_id == principal.firm_id)
    if status:
        stmt = stmt.where(Campaign.status == status)
    return [comms.campaign_view(c, principal.lang) for c in db.execute(stmt.order_by(Campaign.id.desc())).scalars()]


def _campaign_or_404(db: Session, principal: Principal, campaign_id: int) -> Campaign:
    c = db.get(Campaign, campaign_id)
    if c is None or c.firm_id != principal.firm_id:
        raise api_error(404, "NOT_FOUND", "Kampanye tidak ditemukan")
    return c


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, db: Session = Depends(get_db),
                 principal: Principal = Depends(guarded(Permission.COMM_DRAFT))):
    c = _campaign_or_404(db, principal, campaign_id)
    view = comms.campaign_view(c, principal.lang)
    view["communications"] = [comms.communication_view(m, principal.lang) for m in c.communications]
    return view


@router.get("/campaigns/{campaign_id}/preview/{communication_id}")
def preview(campaign_id: int, communication_id: int, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
            principal: Principal = Depends(guarded(Permission.COMM_DRAFT))):
    c = _campaign_or_404(db, principal, campaign_id)
    comm = db.get(Communication, communication_id)
    if comm is None or comm.campaign_id != c.id:
        raise api_error(404, "NOT_FOUND", "Komunikasi tidak ditemukan")
    profile = contact_service.compliance_profile(db, comm.contact)
    try:
        rendered = comms.render_communication(db, ctx, principal.firm, comm, ctx.today(), profile.lawful_basis)
    except TemplateValidationError as exc:
        raise api_error(422, "TEMPLATE_INVALID", str(exc), errors=exc.errors) from exc
    return {"subject": rendered.subject, "text": rendered.text, "html": rendered.html,
            "template_hash": rendered.template_hash}


@router.post("/campaigns/{campaign_id}/submit")
def submit_campaign(campaign_id: int, db: Session = Depends(get_db),
                    principal: Principal = Depends(guarded(Permission.COMM_DRAFT))):
    c = _campaign_or_404(db, principal, campaign_id)
    try:
        comms.submit_campaign(db, c, principal.actor)
    except comms.CommunicationError as exc:
        raise _err(exc) from exc
    db.commit()
    return comms.campaign_view(c, principal.lang)


class ReviewIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=4000)


@router.post("/campaigns/{campaign_id}/review")
def review_campaign(campaign_id: int, body: ReviewIn, db: Session = Depends(get_db),
                    principal: Principal = Depends(guarded(Permission.COMM_APPROVE))):
    c = _campaign_or_404(db, principal, campaign_id)
    try:
        comms.review_campaign(db, c, principal.actor, body.approve, body.note)
    except comms.CommunicationError as exc:
        raise _err(exc) from exc
    db.commit()
    return comms.campaign_view(c, principal.lang)


@router.post("/campaigns/{campaign_id}/send")
def send_campaign(campaign_id: int, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                  principal: Principal = Depends(guarded(Permission.COMM_SEND))):
    c = _campaign_or_404(db, principal, campaign_id)
    try:
        result = comms.send_campaign(db, ctx, principal.firm, c, principal.actor, ctx.today())
    except comms.CommunicationError as exc:
        raise _err(exc) from exc
    except TemplateValidationError as exc:
        raise api_error(422, "TEMPLATE_INVALID", str(exc), errors=exc.errors) from exc
    db.commit()
    view = comms.campaign_view(c, principal.lang)
    view["result"] = result
    view["communications"] = [comms.communication_view(m, principal.lang) for m in c.communications]
    return view


@router.get("/communications")
def list_communications(status: str | None = None, contact_id: int | None = None,
                        limit: int = Query(default=100, ge=1, le=1000), db: Session = Depends(get_db),
                        principal: Principal = Depends(guarded(Permission.COMM_DRAFT))):
    stmt = select(Communication).where(Communication.firm_id == principal.firm_id)
    if status:
        stmt = stmt.where(Communication.status == status)
    if contact_id:
        stmt = stmt.where(Communication.contact_id == contact_id)
    return [comms.communication_view(m, principal.lang)
            for m in db.execute(stmt.order_by(Communication.id.desc()).limit(limit)).scalars()]


@router.get("/purposes")
def purposes(principal: Principal = Depends(guarded(Permission.COMM_DRAFT))):
    return [{"code": p.value, "label": label(p, principal.lang), "template": TEMPLATE_KEYS[p]} for p in Purpose]

