"""Patents, number lookup, bulk import, owners, alerts and reports."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...db.models import Alert, AnnuityPayment, Contact, Owner, Patent, WorkingStatementFiling, utcnow
from ...domain.enums import DataSource, LegalStatus, PatentType
from ...domain.i18n import label, warning_text
from ...domain.matching import OwnerMatcher
from ...domain.numbers import PatentNumberError, parse_patent_number
from ...ingestion.records import PatentRecord
from ...ingestion.sources import PdkiAssistedLookup
from ...security.rbac import Permission
from ...services import audit
from ...services import batch, reports
from ...services.context import AppContext
from ...services.patents import (
    PatentInputError, apply_override, compute_for, find_patent, mark_verified, recompute, upsert_patent,
)
from ...services.views import alert_view, patent_detail, patent_summary
from ..deps import Principal, api_error, get_ctx, get_db, guarded

router = APIRouter(tags=["portfolio"])
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _patent_or_404(db: Session, principal: Principal, patent_id: int) -> Patent:
    p = db.get(Patent, patent_id)
    if p is None or p.firm_id != principal.firm_id:
        raise api_error(404, "NOT_FOUND", "Paten tidak ditemukan")
    return p


def _input_error(exc: PatentInputError):
    return api_error(422, exc.code, str(exc))


# ------------------------------------------------------------------ lookup
@router.get("/lookup")
def lookup(number: str = Query(max_length=60), db: Session = Depends(get_db),
           principal: Principal = Depends(guarded(Permission.PATENT_READ))):
    try:
        n = parse_patent_number(number)
    except PatentNumberError as exc:
        raise api_error(422, "INVALID_NUMBER", str(exc)) from exc
    app = n.normalized if n.kind.value == "APPLICATION" else None
    reg = n.normalized if n.kind.value == "REGISTRATION" else None
    existing = find_patent(db, principal.firm_id, app, reg) if (app or reg) else None
    if existing is None and n.kind.value.startswith("PCT"):
        existing = db.execute(select(Patent).where(
            Patent.firm_id == principal.firm_id,
            or_(Patent.pct_application_number == n.normalized, Patent.pct_publication_number == n.normalized))).scalar()
    return {
        "input": number,
        "normalized": n.normalized,
        "kind": n.kind.value,
        "patent_type": n.patent_type.value if n.patent_type else None,
        "patent_type_label": label(n.patent_type, principal.lang) if n.patent_type else None,
        "is_pct_national_phase": n.is_pct_national_phase,
        "warnings": [{"code": w, "text": warning_text(w, principal.lang)} for w in n.warnings],
        "pdki_search_url": PdkiAssistedLookup().search_url(n) if n.is_indonesian else None,
        "patent": patent_summary(existing, principal.lang) if existing else None,
    }


class PatentIn(BaseModel):
    application_number: str | None = Field(default=None, max_length=40)
    registration_number: str | None = Field(default=None, max_length=40)
    patent_type: PatentType | None = None
    title: str | None = Field(default=None, max_length=2000)
    filing_date: date | None = None
    grant_date: date | None = None
    recorded_status: LegalStatus | None = None
    owner_name: str | None = Field(default=None, max_length=400)
    owner_country: str | None = Field(default=None, max_length=2)
    paid_through_year: int | None = Field(default=None, ge=0, le=20)
    pct_application_number: str | None = Field(default=None, max_length=40)
    pct_publication_number: str | None = Field(default=None, max_length=40)
    priority_claims: list[dict] = Field(default_factory=list)
    agent_name: str | None = Field(default=None, max_length=300)
    source: DataSource = DataSource.USER_INPUT
    source_reference: str | None = Field(default=None, max_length=1000)
    verified_on: date | None = None

    def record(self) -> PatentRecord:
        return PatentRecord(**self.model_dump())


@router.post("/patents", status_code=201)
def create_or_update_patent(body: PatentIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                            principal: Principal = Depends(guarded(Permission.PATENT_WRITE))):
    if body.source in (DataSource.MANUAL_VERIFIED, DataSource.PDKI, DataSource.SAKI) and not principal.can(Permission.PATENT_VERIFY):
        raise api_error(403, "FORBIDDEN", "Hanya pengguna dengan hak verifikasi yang dapat menandai data resmi")
    try:
        result = upsert_patent(db, ctx, principal.firm, body.record(), principal.actor, ctx.today())
    except PatentInputError as exc:
        raise _input_error(exc) from exc
    db.commit()
    comp = compute_for(ctx, result.patent, ctx.today())
    return {
        "created": result.created,
        "changed_fields": result.changed_fields,
        "warnings": [{"code": w, "text": warning_text(w, principal.lang)} for w in result.warnings],
        "owner_needs_review": result.owner_needs_review,
        "new_alerts": len(result.new_alerts),
        "patent": patent_detail(result.patent, comp, principal.lang),
    }


@router.post("/patents/import")
async def import_patents(file: UploadFile = File(...), source: DataSource = Form(DataSource.USER_INPUT),
                         ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                         principal: Principal = Depends(guarded(Permission.PATENT_IMPORT))):
    if source in (DataSource.MANUAL_VERIFIED, DataSource.PDKI, DataSource.SAKI) and not principal.can(Permission.PATENT_VERIFY):
        raise api_error(403, "FORBIDDEN", "Hanya pengguna dengan hak verifikasi yang dapat mengimpor data resmi")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise api_error(413, "FILE_TOO_LARGE", "Ukuran file maksimum 10 MB")
    try:
        summary = batch.import_file(db, ctx, principal.firm, file.filename or "upload.csv", content, source,
                                    principal.actor, ctx.today())
    except PatentInputError as exc:
        raise _input_error(exc) from exc
    db.commit()
    return summary


class PdkiConfirmIn(BaseModel):
    fields: dict
    retrieved_on: date | None = None


@router.post("/lookup/pdki-confirm", status_code=201)
def pdki_confirm(body: PdkiConfirmIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                 principal: Principal = Depends(guarded(Permission.PATENT_WRITE, Permission.PATENT_VERIFY))):
    try:
        rec = PdkiAssistedLookup().record_from_operator(body.fields, retrieved_on=body.retrieved_on or ctx.today())
        result = upsert_patent(db, ctx, principal.firm, rec, principal.actor, ctx.today())
    except PatentInputError as exc:
        raise _input_error(exc) from exc
    except ValueError as exc:
        raise api_error(422, "INVALID_FIELDS", str(exc)) from exc
    db.commit()
    return {"created": result.created, "patent": patent_summary(result.patent, principal.lang)}


@router.get("/patents")
def list_patents(
    status: LegalStatus | None = None,
    patent_type: PatentType | None = None,
    owner_id: int | None = None,
    annuity_within_days: int | None = Query(default=None, ge=0, le=3650),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: AppContext = Depends(get_ctx),
    db: Session = Depends(get_db),
    principal: Principal = Depends(guarded(Permission.PATENT_READ)),
):
    stmt = select(Patent).where(Patent.firm_id == principal.firm_id)
    if status:
        stmt = stmt.where(Patent.computed_status == status.value)
    if patent_type:
        stmt = stmt.where(Patent.patent_type == patent_type.value)
    if owner_id:
        stmt = stmt.where(Patent.owner_id == owner_id)
    if annuity_within_days is not None:
        stmt = stmt.where(Patent.next_annuity_due <= ctx.today() + timedelta(days=annuity_within_days))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Patent.application_number.ilike(like), Patent.registration_number.ilike(like),
                              Patent.title.ilike(like)))
    stmt = stmt.order_by(Patent.next_annuity_due.is_(None), Patent.next_annuity_due, Patent.id).limit(limit).offset(offset)
    return [patent_summary(p, principal.lang) for p in db.execute(stmt).scalars()]


@router.get("/patents/{patent_id}")
def get_patent(patent_id: int, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
               principal: Principal = Depends(guarded(Permission.PATENT_READ))):
    p = _patent_or_404(db, principal, patent_id)
    return patent_detail(p, compute_for(ctx, p, ctx.today()), principal.lang)


class OverrideIn(BaseModel):
    field: str = Field(max_length=40)
    value: str | int | None = None
    reason: str = Field(min_length=5, max_length=2000)


@router.post("/patents/{patent_id}/override")
def override_field(patent_id: int, body: OverrideIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                   principal: Principal = Depends(guarded(Permission.PATENT_VERIFY))):
    p = _patent_or_404(db, principal, patent_id)
    try:
        comp = apply_override(db, ctx, principal.firm, p, body.field, body.value, body.reason, principal.actor, ctx.today())
    except PatentInputError as exc:
        raise _input_error(exc) from exc
    except ValueError as exc:
        raise api_error(422, "INVALID_VALUE", str(exc)) from exc
    db.commit()
    return patent_detail(p, comp, principal.lang)


class VerifyIn(BaseModel):
    source: DataSource = DataSource.MANUAL_VERIFIED
    reference: str | None = Field(default=None, max_length=1000)


@router.post("/patents/{patent_id}/verify")
def verify_patent(patent_id: int, body: VerifyIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                  principal: Principal = Depends(guarded(Permission.PATENT_VERIFY))):
    p = _patent_or_404(db, principal, patent_id)
    comp = mark_verified(db, ctx, principal.firm, p, body.source, body.reference, principal.actor, ctx.today())
    db.commit()
    return patent_detail(p, comp, principal.lang)


class PaymentIn(BaseModel):
    first_year: int = Field(ge=1, le=20)
    last_year: int = Field(ge=1, le=20)
    paid_on: date
    amount_idr: int | None = Field(default=None, ge=0)
    surcharge_idr: int | None = Field(default=None, ge=0)
    reference: str | None = Field(default=None, max_length=200)
    source: DataSource = DataSource.USER_INPUT


@router.post("/patents/{patent_id}/payments", status_code=201)
def record_payment(patent_id: int, body: PaymentIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                   principal: Principal = Depends(guarded(Permission.PATENT_WRITE))):
    p = _patent_or_404(db, principal, patent_id)
    if body.last_year < body.first_year:
        raise api_error(422, "INVALID_YEARS", "Tahun akhir harus >= tahun awal")
    p.payments.append(AnnuityPayment(first_year=body.first_year, last_year=body.last_year, paid_on=body.paid_on,
                                     amount_idr=body.amount_idr, surcharge_idr=body.surcharge_idr,
                                     reference=body.reference, source=body.source.value, recorded_by=principal.user.id))
    db.flush()
    comp, _ = recompute(db, ctx, principal.firm, p, ctx.today())
    audit.record(db, principal.actor, "patent.payment", "patent", p.id,
                 {"years": [body.first_year, body.last_year], "paid_on": body.paid_on.isoformat(),
                  "reference": body.reference}, principal.firm_id)
    db.commit()
    return patent_detail(p, comp, principal.lang)


class WorkingStatementIn(BaseModel):
    period: str = Field(min_length=2, max_length=10)
    submitted_on: date
    declaration: str = Field(pattern="^(MANUFACTURED|IMPORTED|LICENSED|NOT_WORKED)$")
    reason: str | None = Field(default=None, max_length=4000)
    reference: str | None = Field(default=None, max_length=200)


@router.post("/patents/{patent_id}/working-statements", status_code=201)
def record_working_statement(patent_id: int, body: WorkingStatementIn, ctx: AppContext = Depends(get_ctx),
                             db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.PATENT_WRITE))):
    p = _patent_or_404(db, principal, patent_id)
    if body.declaration == "NOT_WORKED" and not (body.reason and body.reason.strip()):
        raise api_error(422, "REASON_REQUIRED", "Alasan wajib diisi apabila paten belum dilaksanakan")
    if any(f.period_key == body.period for f in p.ws_filings):
        raise api_error(409, "ALREADY_RECORDED", "Pernyataan untuk periode ini sudah dicatat")
    p.ws_filings.append(WorkingStatementFiling(period_key=body.period, submitted_on=body.submitted_on,
                                               declaration=body.declaration, reason=body.reason,
                                               reference=body.reference, recorded_by=principal.user.id))
    db.flush()
    comp, _ = recompute(db, ctx, principal.firm, p, ctx.today())
    audit.record(db, principal.actor, "patent.working_statement", "patent", p.id,
                 {"period": body.period, "declaration": body.declaration}, principal.firm_id)
    db.commit()
    return patent_detail(p, comp, principal.lang)


# ------------------------------------------------------------------ owners
def _owner_view(o: Owner, lang: str) -> dict:
    from ...domain.enums import OwnerType

    return {"id": o.id, "name": o.name, "normalized_key": o.normalized_key, "owner_type": o.owner_type,
            "owner_type_label": label(OwnerType(o.owner_type), lang), "country": o.country,
            "needs_review": o.needs_review, "match_suggestions": o.match_suggestions}


@router.get("/owners")
def list_owners(needs_review: bool | None = None, db: Session = Depends(get_db),
                principal: Principal = Depends(guarded(Permission.OWNER_READ))):
    stmt = select(Owner).where(Owner.firm_id == principal.firm_id)
    if needs_review is not None:
        stmt = stmt.where(Owner.needs_review.is_(needs_review))
    return [_owner_view(o, principal.lang) for o in db.execute(stmt.order_by(Owner.name)).scalars()]


class MatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=400)
    country: str | None = Field(default=None, max_length=2)


@router.post("/owners/match")
def match_owner(body: MatchIn, db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.OWNER_READ))):
    matcher = OwnerMatcher()
    for o in db.execute(select(Owner).where(Owner.firm_id == principal.firm_id)).scalars():
        matcher.add(o.id, o.name, o.country)
    return [
        {"owner_id": m.candidate_id, "name": m.candidate_name, "score": m.score, "band": m.band.value,
         "band_label": label(m.band, principal.lang), "reasons": list(m.reasons)}
        for m in matcher.match(body.name, body.country)
    ]


class ResolveIn(BaseModel):
    merge_into_owner_id: int | None = None  # None = confirm as a distinct owner


@router.post("/owners/{owner_id}/resolve")
def resolve_owner(owner_id: int, body: ResolveIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                  principal: Principal = Depends(guarded(Permission.OWNER_WRITE))):
    owner = db.get(Owner, owner_id)
    if owner is None or owner.firm_id != principal.firm_id:
        raise api_error(404, "NOT_FOUND", "Pemegang tidak ditemukan")
    if body.merge_into_owner_id is None:
        owner.needs_review = False
        owner.match_suggestions = []
        audit.record(db, principal.actor, "owner.confirm_distinct", "owner", owner.id, {}, principal.firm_id)
        db.commit()
        return _owner_view(owner, principal.lang)
    target = db.get(Owner, body.merge_into_owner_id)
    if target is None or target.firm_id != principal.firm_id or target.id == owner.id:
        raise api_error(422, "INVALID_TARGET", "Pemegang tujuan tidak valid")
    moved = 0
    # A merge corrects a duplicate record; it is not an ownership change, so no alert is raised.
    for p in db.execute(select(Patent).where(Patent.owner_id == owner.id)).scalars():
        p.owner_id = target.id
        moved += 1
    for c in db.execute(select(Contact).where(Contact.owner_id == owner.id)).scalars():
        c.owner_id = target.id
    db.flush()
    audit.record(db, principal.actor, "owner.merge", "owner", owner.id,
                 {"into": target.id, "name": owner.name, "patents_moved": moved}, principal.firm_id)
    db.delete(owner)
    db.commit()
    return _owner_view(target, principal.lang)


# ------------------------------------------------------------------ alerts
@router.get("/alerts")
def list_alerts(status: str = Query(default="OPEN", pattern="^(OPEN|ACKNOWLEDGED|RESOLVED|ALL)$"),
                severity: str | None = Query(default=None, pattern="^(INFO|WARNING|CRITICAL)$"),
                limit: int = Query(default=100, ge=1, le=1000), db: Session = Depends(get_db),
                principal: Principal = Depends(guarded(Permission.ALERT_READ))):
    stmt = select(Alert).where(Alert.firm_id == principal.firm_id)
    if status != "ALL":
        stmt = stmt.where(Alert.status == status)
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    stmt = stmt.order_by(Alert.target_date, Alert.id).limit(limit)
    return [alert_view(a, principal.lang) for a in db.execute(stmt).scalars()]


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int, db: Session = Depends(get_db),
                      principal: Principal = Depends(guarded(Permission.ALERT_MANAGE))):
    a = db.get(Alert, alert_id)
    if a is None or a.firm_id != principal.firm_id:
        raise api_error(404, "NOT_FOUND", "Peringatan tidak ditemukan")
    a.status = "ACKNOWLEDGED"
    a.acknowledged_by = principal.user.id
    a.acknowledged_at = utcnow()
    audit.record(db, principal.actor, "alert.acknowledge", "alert", a.id, {}, principal.firm_id)
    db.commit()
    return alert_view(a, principal.lang)


class RunIn(BaseModel):
    as_of: date | None = None


@router.post("/alerts/run")
def run_alerts(body: RunIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
               principal: Principal = Depends(guarded(Permission.ALERT_MANAGE))):
    result = batch.run_daily(db, ctx, principal.firm, body.as_of or ctx.today(), principal.actor)
    db.commit()
    return result


# ------------------------------------------------------------------ reports
@router.get("/reports/dashboard")
def dashboard(ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
              principal: Principal = Depends(guarded(Permission.REPORT_READ))):
    return reports.dashboard(db, principal.firm_id, ctx.today(), principal.lang)


@router.get("/reports/portfolio.csv")
def portfolio_csv(db: Session = Depends(get_db), principal: Principal = Depends(guarded(Permission.REPORT_READ))):
    audit.record(db, principal.actor, "report.export", "firm", principal.firm_id, {"lang": principal.lang},
                 principal.firm_id)
    db.commit()
    data = reports.portfolio_csv(db, principal.firm_id, principal.lang)
    name = "portofolio-paten.csv" if principal.lang == "id" else "patent-portfolio.csv"
    return Response(content="﻿" + data, media_type="text/csv; charset=utf-8",
                     headers={"Content-Disposition": f'attachment; filename="{name}"'})
