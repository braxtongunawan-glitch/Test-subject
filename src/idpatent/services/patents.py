"""Patent upsert, provenance handling, recomputation and alert persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Alert, Firm, Owner, Patent, utcnow
from ..domain.alerts import AlertCandidate, StatusSnapshot, detect_changes, evaluate_alerts
from ..domain.calculator import PatentComputation, PatentFacts, compute
from ..domain.enums import AlertKind, DataSource, LegalStatus, NumberKind, PatentType, VerificationStatus
from ..domain.matching import OwnerMatcher, normalize_owner_name
from ..domain.numbers import PatentNumberError, parse_patent_number, try_parse_patent_number
from ..domain.rules import AlertPolicy
from ..ingestion.records import PatentRecord
from . import audit
from .audit import Actor
from .context import AppContext

SOURCE_RANK = {
    DataSource.MANUAL_VERIFIED: 5,
    DataSource.AGENT_FEED: 4,
    DataSource.PDKI: 3,
    DataSource.SAKI: 3,
    DataSource.USER_INPUT: 2,
    DataSource.SUPPLEMENTARY: 1,
}
VERIFYING_SOURCES = {DataSource.MANUAL_VERIFIED, DataSource.AGENT_FEED, DataSource.PDKI, DataSource.SAKI}
OVERRIDABLE_FIELDS = {"filing_date", "grant_date", "patent_type", "recorded_status", "paid_through_year"}
TIME_BASED_KINDS = {
    AlertKind.EXPIRY_APPROACHING.value, AlertKind.ANNUITY_DUE.value, AlertKind.ANNUITY_GRACE.value,
    AlertKind.ANNUITY_PAYMENT_UNVERIFIED.value, AlertKind.WORKING_STATEMENT_DUE.value,
    AlertKind.WORKING_STATEMENT_OVERDUE.value,
}


class PatentInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class UpsertResult:
    patent: Patent
    created: bool
    changed_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    owner_needs_review: bool = False
    new_alerts: list[Alert] = field(default_factory=list)


def alert_policy_for(ctx: AppContext, firm: Firm) -> AlertPolicy:
    return AlertPolicy.from_dict(firm.alert_policy) if firm.alert_policy else ctx.default_alert_policy


# ---------------------------------------------------------------- owners
class OwnerResolver:
    """Resolves owner names to Owner rows for one firm, reusing an index across a batch."""

    def __init__(self, session: Session, firm_id: int):
        self.session, self.firm_id = session, firm_id
        self.matcher = OwnerMatcher()
        self._by_id: dict[int, Owner] = {}
        for owner in session.execute(select(Owner).where(Owner.firm_id == firm_id)).scalars():
            self.matcher.add(owner.id, owner.name, owner.country)
            self._by_id[owner.id] = owner

    def resolve(self, name: str, country: str | None = None) -> tuple[Owner, bool]:
        """Return ``(owner, needs_review)``; creates the owner when no confident match exists."""
        matches = self.matcher.match(name, country)
        if matches and matches[0].band.value == "HIGH":
            return self._by_id[matches[0].candidate_id], False
        norm = normalize_owner_name(name, country)
        suggestions = [
            {"owner_id": m.candidate_id, "name": m.candidate_name, "score": m.score, "reasons": list(m.reasons)}
            for m in matches
            if m.band.value == "MEDIUM"
        ]
        owner = Owner(
            firm_id=self.firm_id,
            name=name.strip(),
            normalized_key=norm.key,
            owner_type=norm.owner_type.value,
            country=(country or None),
            legal_forms=sorted(norm.legal_forms),
            needs_review=bool(suggestions),
            match_suggestions=suggestions,
        )
        self.session.add(owner)
        self.session.flush()
        self.matcher.add(owner.id, owner.name, owner.country)
        self._by_id[owner.id] = owner
        return owner, bool(suggestions)


# ---------------------------------------------------------------- facts & recompute
def effective_value(patent: Patent, name: str):
    ov = (patent.overrides or {}).get(name)
    return ov["value"] if ov else getattr(patent, name)


def facts_for(patent: Patent) -> PatentFacts:
    def as_date(v):
        return date.fromisoformat(v) if isinstance(v, str) else v

    paid = effective_value(patent, "paid_through_year")
    # Recorded payments are confirmed evidence and always count, on top of any source value or override.
    payment_years = [p.last_year for p in patent.payments]
    if payment_years:
        paid = max(paid or 0, max(payment_years))
    recorded = effective_value(patent, "recorded_status")
    source = DataSource(patent.source)
    if patent.overrides:
        source = DataSource.MANUAL_VERIFIED
    return PatentFacts(
        patent_type=PatentType(effective_value(patent, "patent_type")),
        filing_date=as_date(effective_value(patent, "filing_date")),
        grant_date=as_date(effective_value(patent, "grant_date")),
        recorded_status=LegalStatus(recorded) if recorded else None,
        paid_through_year=paid,
        working_statements_submitted=frozenset(f.period_key for f in patent.ws_filings),
        source=source,
        last_verified_on=patent.last_verified_on,
    )


def compute_for(ctx: AppContext, patent: Patent, as_of: date) -> PatentComputation:
    return compute(facts_for(patent), ctx.registry, as_of)


def snapshot(patent: Patent) -> StatusSnapshot | None:
    if not patent.computed_status:
        return None
    return StatusSnapshot(LegalStatus(patent.computed_status), str(patent.owner_id) if patent.owner_id else None)


def recompute(
    session: Session,
    ctx: AppContext,
    firm: Firm,
    patent: Patent,
    as_of: date,
    prev: StatusSnapshot | None = None,
    *,
    use_cached_prev: bool = True,
) -> tuple[PatentComputation, list[Alert]]:
    if prev is None and use_cached_prev:
        prev = snapshot(patent)
    comp = compute_for(ctx, patent, as_of)
    nxt, nws = comp.next_annuity, comp.next_working_statement
    patent.protection_end = comp.protection_end
    patent.computed_status = comp.status.value
    patent.next_annuity_due = nxt.due_date if nxt else None
    patent.next_grace_end = nxt.grace_end if nxt else None
    patent.next_ws_due = nws.due_date if nws else None
    patent.confidence = comp.confidence
    patent.confidence_band = comp.confidence_band.value
    patent.warnings = comp.warnings
    patent.rules_version = comp.rules_version
    patent.computed_at = utcnow()

    curr = StatusSnapshot(comp.status, str(patent.owner_id) if patent.owner_id else None)
    candidates = evaluate_alerts(comp, alert_policy_for(ctx, firm), as_of) + detect_changes(prev, curr, as_of)
    created = persist_alerts(session, firm.id, patent, candidates)
    return comp, created


def persist_alerts(session: Session, firm_id: int, patent: Patent, candidates: list[AlertCandidate]) -> list[Alert]:
    if patent.id is None:
        session.flush()
    existing = {a.dedupe_key: a for a in session.execute(select(Alert).where(Alert.patent_id == patent.id)).scalars()}
    active_keys = {c.dedupe_key for c in candidates}
    # Supersede time-based alerts whose stage has moved on or whose obligation is resolved.
    for alert in existing.values():
        if alert.status == "OPEN" and alert.kind in TIME_BASED_KINDS and alert.dedupe_key not in active_keys:
            alert.status = "RESOLVED"
    created = []
    for c in candidates:
        if c.dedupe_key in existing:
            continue
        alert = Alert(
            firm_id=firm_id, patent_id=patent.id, kind=c.kind.value, reference=c.reference, stage=c.stage,
            dedupe_key=c.dedupe_key, target_date=c.target_date, trigger_date=c.trigger_date, severity=c.severity.value,
        )
        session.add(alert)
        created.append(alert)
    session.flush()
    return created


# ---------------------------------------------------------------- upsert
def _parse_numbers(rec: PatentRecord) -> tuple[str | None, str | None, PatentType | None, bool, list[str]]:
    warnings: list[str] = []
    app = reg = None
    number_type: PatentType | None = None
    pct = False
    for raw in (rec.application_number, rec.registration_number):
        if not raw:
            continue
        try:
            n = parse_patent_number(raw)
        except PatentNumberError as exc:
            raise PatentInputError("INVALID_NUMBER", str(exc)) from exc
        warnings.extend(n.warnings)
        if n.kind == NumberKind.APPLICATION:
            app = n.normalized
            number_type = n.patent_type or number_type
            pct = pct or n.is_pct_national_phase
        elif n.kind == NumberKind.REGISTRATION:
            reg = n.normalized
            number_type = number_type or n.patent_type
        else:
            raise PatentInputError("NOT_AN_INDONESIAN_NUMBER", f"{raw!r} bukan nomor paten Indonesia")
    if not app and not reg:
        raise PatentInputError("NUMBER_REQUIRED", "Nomor permohonan atau nomor paten wajib diisi")
    return app, reg, number_type, pct, warnings


def find_patent(session: Session, firm_id: int, app: str | None, reg: str | None) -> Patent | None:
    by_app = by_reg = None
    if app:
        by_app = session.execute(select(Patent).where(Patent.firm_id == firm_id, Patent.application_number == app)).scalar()
    if reg:
        by_reg = session.execute(select(Patent).where(Patent.firm_id == firm_id, Patent.registration_number == reg)).scalar()
    if by_app and by_reg and by_app.id != by_reg.id:
        raise PatentInputError("NUMBER_CONFLICT", f"{app} dan {reg} tercatat pada paten yang berbeda")
    if by_app and reg and by_app.registration_number and by_app.registration_number != reg:
        raise PatentInputError("NUMBER_CONFLICT", f"{app} sudah tercatat dengan nomor paten {by_app.registration_number}")
    if by_reg and app and by_reg.application_number and by_reg.application_number != app:
        raise PatentInputError("NUMBER_CONFLICT", f"{reg} sudah tercatat dengan nomor permohonan {by_reg.application_number}")
    return by_app or by_reg


def _jsonable(v):
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "value"):
        return v.value
    return v


def upsert_patent(
    session: Session,
    ctx: AppContext,
    firm: Firm,
    rec: PatentRecord,
    actor: Actor,
    as_of: date,
    *,
    owners: OwnerResolver | None = None,
) -> UpsertResult:
    app, reg, number_type, pct_from_number, warnings = _parse_numbers(rec)
    patent = find_patent(session, firm.id, app, reg)
    source = rec.source

    if source == DataSource.SUPPLEMENTARY:
        if patent is None:
            raise PatentInputError("SUPPLEMENTARY_CANNOT_CREATE", "Sumber tambahan hanya untuk data prioritas/famili")
        changed = _apply_family_fields(patent, rec)
        if changed:
            audit.record(session, actor, "patent.family_update", "patent", patent.id, {"fields": changed}, firm.id)
        comp, alerts = recompute(session, ctx, firm, patent, as_of)
        return UpsertResult(patent, False, changed, warnings, False, alerts)

    ptype = number_type or rec.patent_type
    if rec.patent_type and number_type and rec.patent_type != number_type:
        warnings.append("TYPE_CONFLICT")
    if ptype is None:
        raise PatentInputError("TYPE_REQUIRED", "Jenis paten tidak dapat ditentukan; isi kolom jenis")

    created = patent is None
    if created:
        if rec.filing_date is None:
            raise PatentInputError("FILING_DATE_REQUIRED", "Tanggal penerimaan wajib diisi untuk paten baru")
        patent = Patent(firm_id=firm.id, patent_type=ptype.value, filing_date=rec.filing_date, source=source.value,
                        overrides={}, priority_claims=[], warnings=[])
        session.add(patent)
        prev = None
    else:
        prev = snapshot(patent)

    existing_rank = SOURCE_RANK[DataSource(patent.source)] if not created else 0
    lower_priority = (
        not created
        and SOURCE_RANK[source] < existing_rank
        and patent.verification_status == VerificationStatus.VERIFIED.value
    )
    changed: dict[str, list] = {}

    def assign(name: str, value):
        if value is None:
            return
        current = getattr(patent, name)
        if current == value:
            return
        if lower_priority and current is not None:
            if "LOWER_PRIORITY_SOURCE_FIELDS_SKIPPED" not in warnings:
                warnings.append("LOWER_PRIORITY_SOURCE_FIELDS_SKIPPED")
            return
        changed[name] = [_jsonable(current), _jsonable(value)]
        setattr(patent, name, value)

    assign("application_number", app)
    assign("registration_number", reg)
    assign("patent_type", ptype.value)
    assign("title", rec.title)
    assign("filing_date", rec.filing_date)
    assign("grant_date", rec.grant_date)
    assign("recorded_status", rec.recorded_status.value if rec.recorded_status else None)
    assign("paid_through_year", rec.paid_through_year)
    assign("agent_name", rec.agent_name)
    if pct_from_number and not patent.is_pct_national_phase:
        patent.is_pct_national_phase = True
        changed["is_pct_national_phase"] = [False, True]
    for name, value in _apply_family_fields(patent, rec, return_values=True).items():
        changed[name] = value

    if rec.owner_name:
        resolver = owners or OwnerResolver(session, firm.id)
        owner, review = resolver.resolve(rec.owner_name, rec.owner_country)
        if patent.owner_id != owner.id and not (lower_priority and patent.owner_id):
            changed["owner_id"] = [patent.owner_id, owner.id]
            patent.owner_id = owner.id
    else:
        review = False

    if patent.is_pct_national_phase and created:
        warnings.append("PCT_FILING_DATE_CHECK")

    if not lower_priority:
        patent.source = source.value
        patent.source_reference = rec.source_reference or patent.source_reference
        verified_on = rec.verified_on or (as_of if source in VERIFYING_SOURCES else None)
        if verified_on:
            patent.last_verified_on = verified_on
        if patent.overrides:
            patent.verification_status = VerificationStatus.OVERRIDDEN.value
        elif source in VERIFYING_SOURCES and patent.last_verified_on:
            patent.verification_status = VerificationStatus.VERIFIED.value
        else:
            patent.verification_status = VerificationStatus.UNVERIFIED.value

    session.flush()
    _, alerts = recompute(session, ctx, firm, patent, as_of, prev, use_cached_prev=False)
    audit.record(
        session, actor, "patent.create" if created else "patent.update", "patent", patent.id,
        {"source": source.value, "changes": changed, "warnings": warnings, "row": rec.row_number}, firm.id,
    )
    return UpsertResult(patent, created, sorted(changed), warnings, review, alerts)


def _apply_family_fields(patent: Patent, rec: PatentRecord, return_values: bool = False):
    changed: dict[str, list] = {}
    if rec.pct_application_number:
        n = try_parse_patent_number(rec.pct_application_number)
        value = n.normalized if n and n.kind == NumberKind.PCT_APPLICATION else rec.pct_application_number.strip()
        if value != patent.pct_application_number:
            changed["pct_application_number"] = [patent.pct_application_number, value]
            patent.pct_application_number = value
            patent.is_pct_national_phase = True
            patent.family_id = patent.family_id or value
    if rec.pct_publication_number:
        n = try_parse_patent_number(rec.pct_publication_number)
        value = n.normalized if n else rec.pct_publication_number.strip()
        if value != patent.pct_publication_number:
            changed["pct_publication_number"] = [patent.pct_publication_number, value]
            patent.pct_publication_number = value
    if rec.priority_claims and rec.priority_claims != patent.priority_claims:
        changed["priority_claims"] = [patent.priority_claims, rec.priority_claims]
        patent.priority_claims = rec.priority_claims
        if not patent.family_id:
            first = rec.priority_claims[0]
            patent.family_id = f"{first.get('country') or ''}{first.get('number') or ''}".replace(" ", "") or None
    return changed if return_values else sorted(changed)


def apply_override(
    session: Session, ctx: AppContext, firm: Firm, patent: Patent, field_name: str, value, reason: str, actor: Actor, as_of: date
) -> PatentComputation:
    if field_name not in OVERRIDABLE_FIELDS:
        raise PatentInputError("FIELD_NOT_OVERRIDABLE", f"Field {field_name} tidak dapat di-override")
    if not reason or len(reason.strip()) < 5:
        raise PatentInputError("REASON_REQUIRED", "Alasan override wajib diisi")
    prev = snapshot(patent)
    overrides = dict(patent.overrides or {})
    if value is None:
        removed = overrides.pop(field_name, None)
        detail = {"field": field_name, "removed": removed, "reason": reason}
    else:
        if field_name == "patent_type":
            value = PatentType(value).value
        elif field_name == "recorded_status":
            value = LegalStatus(value).value
        elif field_name in ("filing_date", "grant_date"):
            value = (value if isinstance(value, date) else date.fromisoformat(str(value))).isoformat()
        elif field_name == "paid_through_year":
            value = int(value)
        overrides[field_name] = {"value": value, "reason": reason.strip(), "by": actor.user_id, "at": utcnow().isoformat()}
        detail = {"field": field_name, "value": value, "original": _jsonable(getattr(patent, field_name)), "reason": reason}
    patent.overrides = overrides
    patent.verification_status = (
        VerificationStatus.OVERRIDDEN.value if overrides else VerificationStatus.UNVERIFIED.value
    )
    session.flush()
    comp, _ = recompute(session, ctx, firm, patent, as_of, prev, use_cached_prev=False)
    audit.record(session, actor, "patent.override", "patent", patent.id, detail, firm.id)
    return comp


def mark_verified(session: Session, ctx: AppContext, firm: Firm, patent: Patent, source: DataSource, reference: str | None,
                  actor: Actor, as_of: date) -> PatentComputation:
    patent.last_verified_on = as_of
    if source in VERIFYING_SOURCES:
        patent.source = source.value
    if reference:
        patent.source_reference = reference
    if not patent.overrides:
        patent.verification_status = VerificationStatus.VERIFIED.value
    comp, _ = recompute(session, ctx, firm, patent, as_of)
    audit.record(session, actor, "patent.verify", "patent", patent.id, {"source": source.value, "reference": reference}, firm.id)
    return comp
