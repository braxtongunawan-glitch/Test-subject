"""Alert engine: turns a computation into time-staged, de-duplicable alerts.

For each target date (expiry, annuity due, grace end, Working Statement due)
the engine emits at most one alert: the most urgent lead-time stage already
reached. Each stage has a stable ``dedupe_key``, so re-running the engine daily
creates a new alert only when a patent enters a new stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .calculator import PatentComputation
from .dates import add_months
from .enums import AlertKind, InstallmentStatus, LegalStatus, ObligationStatus, Severity
from .rules import AlertPolicy

QUIET_STATUSES = frozenset(
    {LegalStatus.REFUSED, LegalStatus.WITHDRAWN, LegalStatus.LAPSED, LegalStatus.EXPIRED, LegalStatus.PENDING}
)
OVERDUE_LOOKBACK_DAYS = 365


@dataclass(frozen=True)
class AlertCandidate:
    kind: AlertKind
    reference: str
    target_date: date
    stage: str
    trigger_date: date
    severity: Severity

    @property
    def dedupe_key(self) -> str:
        return f"{self.kind.value}:{self.reference}:{self.stage}"


@dataclass(frozen=True)
class StatusSnapshot:
    status: LegalStatus
    owner_key: str | None = None


def _severity(target: date, as_of: date, policy: AlertPolicy) -> Severity:
    days = (target - as_of).days
    if days <= policy.critical_within_days:
        return Severity.CRITICAL
    if days <= policy.warning_within_days:
        return Severity.WARNING
    return Severity.INFO


def _stage(target: date, as_of: date, leads: tuple[int, ...], unit: str) -> tuple[str, date] | None:
    if as_of > target:
        return None
    for lead in sorted(leads):
        trigger = add_months(target, -lead) if unit == "m" else target - timedelta(days=lead)
        if trigger <= as_of:
            return f"{lead}{unit}", trigger
    return None


def evaluate_alerts(comp: PatentComputation, policy: AlertPolicy, as_of: date | None = None) -> list[AlertCandidate]:
    as_of = as_of or comp.as_of
    if comp.status in QUIET_STATUSES:
        return []
    out: list[AlertCandidate] = []

    if hit := _stage(comp.protection_end, as_of, policy.expiry_months, "m"):
        out.append(
            AlertCandidate(AlertKind.EXPIRY_APPROACHING, "expiry", comp.protection_end, hit[0], hit[1],
                           _severity(comp.protection_end, as_of, policy))
        )

    for inst in comp.annuities:
        ref = f"annuity:{inst.years_label}"
        if inst.status == InstallmentStatus.UPCOMING:
            if hit := _stage(inst.due_date, as_of, policy.annuity_days, "d"):
                out.append(AlertCandidate(AlertKind.ANNUITY_DUE, ref, inst.due_date, hit[0], hit[1],
                                          _severity(inst.due_date, as_of, policy)))
        elif inst.status == InstallmentStatus.IN_GRACE:
            hit = _stage(inst.grace_end, as_of, policy.grace_end_days, "d")
            stage, trigger = hit or ("grace", inst.due_date + timedelta(days=1))
            out.append(AlertCandidate(AlertKind.ANNUITY_GRACE, ref, inst.grace_end, stage, trigger, Severity.CRITICAL))
        elif inst.status == InstallmentStatus.UNKNOWN and as_of <= inst.grace_end:
            out.append(AlertCandidate(AlertKind.ANNUITY_PAYMENT_UNVERIFIED, ref, inst.grace_end, "verify",
                                      inst.due_date + timedelta(days=1), Severity.WARNING))
        elif inst.status == InstallmentStatus.MISSED:
            out.append(AlertCandidate(AlertKind.LAPSE_RISK, ref, inst.grace_end, "missed",
                                      inst.grace_end + timedelta(days=1), Severity.CRITICAL))

    for ws in comp.working_statements:
        ref = f"ws:{ws.period_key}"
        if ws.status == ObligationStatus.UPCOMING:
            if hit := _stage(ws.due_date, as_of, policy.working_statement_days, "d"):
                out.append(AlertCandidate(AlertKind.WORKING_STATEMENT_DUE, ref, ws.due_date, hit[0], hit[1],
                                          _severity(ws.due_date, as_of, policy)))
        elif ws.status == ObligationStatus.OVERDUE and (as_of - ws.due_date).days <= OVERDUE_LOOKBACK_DAYS:
            out.append(AlertCandidate(AlertKind.WORKING_STATEMENT_OVERDUE, ref, ws.due_date, "overdue",
                                      ws.due_date + timedelta(days=1), Severity.CRITICAL))
    return out


def detect_changes(prev: StatusSnapshot | None, curr: StatusSnapshot, as_of: date) -> list[AlertCandidate]:
    if prev is None:
        return []
    out = []
    if prev.status != curr.status:
        severity = (
            Severity.CRITICAL
            if curr.status in (LegalStatus.LAPSE_SUSPECTED, LegalStatus.LAPSED)
            else Severity.WARNING if curr.status in (LegalStatus.EXPIRED, LegalStatus.REFUSED, LegalStatus.WITHDRAWN)
            else Severity.INFO
        )
        kind = AlertKind.LAPSE_RISK if curr.status == LegalStatus.LAPSE_SUSPECTED else AlertKind.STATUS_CHANGE
        out.append(AlertCandidate(kind, f"status:{prev.status.value}->{curr.status.value}", as_of,
                                  as_of.isoformat(), as_of, severity))
    if prev.owner_key and curr.owner_key and prev.owner_key != curr.owner_key:
        out.append(AlertCandidate(AlertKind.OWNERSHIP_CHANGE, f"owner:{prev.owner_key}->{curr.owner_key}", as_of,
                                  as_of.isoformat(), as_of, Severity.WARNING))
    return out
