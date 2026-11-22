"""Firm settings, user administration, audit log and client portal."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import AuditLog, Contact, Firm, Owner, Patent, User
from ...domain.dates import format_datetime_wib
from ...domain.enums import Purpose, Role
from ...domain.i18n import label
from ...domain.rules import AlertPolicy
from ...security.rbac import Permission
from ...services import audit, reports
from ...services import auth as auth_service
from ...services import contacts as contact_service
from ...services.context import AppContext
from ...services.patents import compute_for
from ...services.views import patent_detail, patent_summary
from ..deps import Principal, api_error, get_ctx, get_db, guarded

router = APIRouter(tags=["admin"])


class FirmIn(BaseModel):
    name: str = Field(max_length=200)
    legal_name: str = Field(max_length=300)
    address: str = Field(min_length=10, max_length=1000)  # physical address, shown in every e-mail
    email: str = Field(max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    website: str | None = Field(default=None, max_length=300)
    services_id: list[str] = Field(default_factory=list, max_length=20)
    services_en: list[str] = Field(default_factory=list, max_length=20)
    alert_policy: dict | None = None
    cold_daily_cap: int = Field(default=50, ge=0, le=1000)
    bulk_threshold: int = Field(default=10, ge=2, le=10000)
    client_agreement_required: bool = False


def _firm_view(f: Firm) -> dict:
    return {"id": f.id, "name": f.name, "legal_name": f.legal_name, "address": f.address, "email": f.email,
            "phone": f.phone, "website": f.website, "services_id": f.services_id, "services_en": f.services_en,
            "alert_policy": f.alert_policy, "cold_daily_cap": f.cold_daily_cap, "bulk_threshold": f.bulk_threshold,
            "client_agreement_required": f.client_agreement_required}


def _validate_policy(policy: dict | None) -> None:
    if policy is not None:
        try:
            AlertPolicy.from_dict(policy)
        except (ValueError, TypeError) as exc:
            raise api_error(422, "INVALID_ALERT_POLICY", str(exc)) from exc


@router.post("/admin/firms", status_code=201)
def create_firm(body: FirmIn, db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.PLATFORM_ADMIN))):
    _validate_policy(body.alert_policy)
    firm = Firm(**body.model_dump())
    db.add(firm)
    db.flush()
    audit.record(db, principal.actor, "firm.create", "firm", firm.id, {"name": firm.name}, firm.id)
    db.commit()
    return _firm_view(firm)


@router.get("/admin/firm")
def get_firm(principal: Principal = Depends(guarded(Permission.SETTINGS_MANAGE))):
    return _firm_view(principal.firm)


@router.put("/admin/firm")
def update_firm(body: FirmIn, db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.SETTINGS_MANAGE))):
    _validate_policy(body.alert_policy)
    firm = db.get(Firm, principal.firm_id)
    for k, v in body.model_dump().items():
        setattr(firm, k, v)
    audit.record(db, principal.actor, "firm.update", "firm", firm.id, {"fields": sorted(body.model_dump())}, firm.id)
    db.commit()
    return _firm_view(firm)


class UserIn(BaseModel):
    email: str = Field(max_length=320)
    name: str = Field(max_length=200)
    role: Role
    password: str = Field(max_length=256)
    language: str = Field(default="id", pattern="^(id|en)$")
    firm_id: int | None = None
    owner_scope: list[int] = Field(default_factory=list)


@router.post("/admin/users", status_code=201)
def create_user(body: UserIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                principal: Principal = Depends(guarded(Permission.USER_MANAGE))):
    platform = principal.can(Permission.PLATFORM_ADMIN)
    if body.role == Role.SUPER_ADMIN and not platform:
        raise api_error(403, "FORBIDDEN", "Tidak dapat membuat Super Admin")
    firm_id = body.firm_id if platform and body.firm_id else principal.firm_id
    if body.role == Role.CLIENT_USER:
        if not body.owner_scope:
            raise api_error(422, "OWNER_SCOPE_REQUIRED", "Pengguna klien harus dibatasi pada pemegang paten tertentu")
        for oid in body.owner_scope:
            owner = db.get(Owner, oid)
            if owner is None or owner.firm_id != firm_id:
                raise api_error(422, "OWNER_NOT_FOUND", f"Pemegang {oid} tidak ditemukan")
    try:
        user = auth_service.create_user(db, ctx, email=body.email, name=body.name, role=body.role, password=body.password,
                                        firm_id=firm_id, language=body.language, owner_scope=body.owner_scope,
                                        actor=principal.actor)
    except auth_service.AuthError as exc:
        raise api_error(exc.status, exc.code, str(exc)) from exc
    db.commit()
    return {"id": user.id, "role": user.role, "firm_id": user.firm_id}


@router.get("/admin/users")
def list_users(ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
               principal: Principal = Depends(guarded(Permission.USER_MANAGE))):
    rows = db.execute(select(User).where(User.firm_id == principal.firm_id).order_by(User.id)).scalars()
    return [{"id": u.id, "name": u.name, "email": auth_service.user_email(ctx, u), "role": u.role,
             "mfa_enabled": u.mfa_enabled, "is_active": u.is_active, "owner_scope": u.owner_scope} for u in rows]


@router.get("/audit")
def audit_log(action: str | None = Query(default=None, max_length=80), limit: int = Query(default=200, ge=1, le=2000),
              db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.AUDIT_READ))):
    stmt = select(AuditLog)
    if not principal.can(Permission.PLATFORM_ADMIN):
        stmt = stmt.where(AuditLog.firm_id == principal.firm_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = db.execute(stmt.order_by(AuditLog.id.desc()).limit(limit)).scalars()
    return [{"id": r.id, "occurred_at": format_datetime_wib(r.occurred_at, principal.lang), "actor": r.actor_label,
             "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id, "details": r.details,
             "ip": r.ip, "entry_hash": r.entry_hash} for r in rows]


@router.get("/audit/verify")
def audit_verify(db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.AUDIT_READ))):
    from ...services import agreements as agreement_service

    a = audit.verify(db)
    b = agreement_service.verify_acceptance_chain(db)
    return {"audit_log": {"ok": a.ok, "checked": a.checked, "first_broken_id": a.first_broken_id},
            "agreement_acceptances": {"ok": b.ok, "checked": b.checked, "first_broken_id": b.first_broken_id}}


# ------------------------------------------------------------------ client portal
def _portal_patent(db: Session, principal: Principal, patent_id: int) -> Patent:
    p = db.get(Patent, patent_id)
    if p is None or p.firm_id != principal.firm_id or p.owner_id not in (principal.user.owner_scope or []):
        raise api_error(404, "NOT_FOUND", "Paten tidak ditemukan")
    return p


@router.get("/portal/patents")
def portal_patents(db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.PORTAL_SELF))):
    scope = principal.user.owner_scope or []
    rows = db.execute(select(Patent).where(Patent.firm_id == principal.firm_id, Patent.owner_id.in_(scope))
                      .order_by(Patent.protection_end)).scalars()
    return [patent_summary(p, principal.lang) for p in rows]


@router.get("/portal/patents/{patent_id}")
def portal_patent(patent_id: int, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                  principal: Principal = Depends(guarded(Permission.PORTAL_SELF))):
    p = _portal_patent(db, principal, patent_id)
    return patent_detail(p, compute_for(ctx, p, ctx.today()), principal.lang)


@router.get("/portal/dashboard")
def portal_dashboard(ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                     principal: Principal = Depends(guarded(Permission.PORTAL_SELF))):
    return reports.dashboard(db, principal.firm_id, ctx.today(), principal.lang, owner_ids=principal.user.owner_scope or [])


@router.get("/portal/export.csv")
def portal_export(db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.PORTAL_SELF))):
    data = reports.portfolio_csv(db, principal.firm_id, principal.lang, owner_ids=principal.user.owner_scope or [])
    return Response(content="﻿" + data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="paten.csv"'})


def _own_contacts(db: Session, ctx: AppContext, principal: Principal) -> list[Contact]:
    email = auth_service.user_email(ctx, principal.user)
    index = ctx.cipher.blind_index(email)
    return list(db.execute(select(Contact).where(Contact.firm_id == principal.firm_id, Contact.email_index == index,
                                                 Contact.erased_at.is_(None))).scalars())


@router.get("/portal/preferences")
def portal_preferences(ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                       principal: Principal = Depends(guarded(Permission.PORTAL_SELF))):
    contacts = _own_contacts(db, ctx, principal)
    opted = set().union(*[set(c.opted_out_purposes or []) for c in contacts]) if contacts else set()
    all_out = any(c.opted_out_all for c in contacts)
    return [{"purpose": p.value, "label": label(p, principal.lang), "receiving": bool(contacts) and not all_out
             and p.value not in opted} for p in Purpose]


class PreferencesIn(BaseModel):
    receive: dict[Purpose, bool]


@router.put("/portal/preferences")
def update_portal_preferences(body: PreferencesIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                              principal: Principal = Depends(guarded(Permission.PORTAL_SELF))):
    contacts = _own_contacts(db, ctx, principal)
    if not contacts:
        raise api_error(404, "NO_CONTACT_RECORD", "Tidak ada data kontak yang terkait dengan akun ini")
    for c in contacts:
        for purpose, want in body.receive.items():
            contact_service.record_consent(db, c, purpose, want, "PREFERENCE_CENTER", principal.actor,
                                           evidence="client portal preference centre")
    db.commit()
    return portal_preferences(ctx, db, principal)
