"""Protection term, annuity (biaya tahunan) and Working Statement calculations.

Model (all parameters come from the effective rule set, see ``rules.py``):

* Protection year *k* runs from ``filing + (k-1) years`` to ``filing + k years``.
* Term: Paten 20 years, Paten Sederhana 10 years from the filing date
  (Tanggal Penerimaan; for PCT national phase, the international filing date).
* First annuity: due ``first_payment_months_after_grant`` months after grant and
  covers protection year 1 through the year in which grant falls, plus
  ``first_payment_extra_years_after_grant_year`` further year(s).
* Each later year *k*: due ``subsequent_months_before_anniversary`` month(s)
  before the anniversary that starts year *k*.
* Grace: ``grace_months`` after the due date, with a surcharge.
* Working Statement: one per calendar year due 31 December (default), or one
  per annuity installment, depending on the rule set's ``mode``.

``paid_through_year`` is the last protection year covered by a *confirmed*
payment. When it is unknown, past installments are reported as ``UNKNOWN``
rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .dates import add_months, add_years, anniversaries_passed
from .enums import (
    TERMINAL_RECORDED_STATUSES,
    ConfidenceBand,
    DataSource,
    InstallmentStatus,
    LegalStatus,
    ObligationStatus,
    PatentType,
    TermEndConvention,
    WorkingStatementMode,
)
from .rules import RuleRegistry, RuleSet


class CalculationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PatentFacts:
    patent_type: PatentType
    filing_date: date
    grant_date: date | None = None
    recorded_status: LegalStatus | None = None
    paid_through_year: int | None = None
    working_statements_submitted: frozenset[str] = frozenset()
    source: DataSource = DataSource.USER_INPUT
    last_verified_on: date | None = None


@dataclass(frozen=True)
class AnnuityInstallment:
    sequence: int
    first_year: int
    last_year: int
    coverage_start: date
    coverage_end: date
    due_date: date
    grace_end: date
    surcharge_percent: int
    grace_requires_request: bool
    status: InstallmentStatus
    rule_id: str

    @property
    def years_label(self) -> str:
        """Protection years covered, e.g. ``Y1-6`` for the first payment or ``Y13``."""
        if self.first_year == self.last_year:
            return f"Y{self.last_year}"
        return f"Y{self.first_year}-{self.last_year}"

    @property
    def outstanding(self) -> bool:
        return self.status in (InstallmentStatus.UPCOMING, InstallmentStatus.IN_GRACE, InstallmentStatus.MISSED)


@dataclass(frozen=True)
class WorkingStatementObligation:
    period_key: str  # "2025" (calendar mode) or "A3" (annuity mode, installment 3)
    due_date: date
    status: ObligationStatus
    rule_id: str
    final_partial_year: bool = False


@dataclass
class PatentComputation:
    as_of: date
    patent_type: PatentType
    term_years: int
    protection_start: date
    protection_end: date
    status: LegalStatus
    annuities: list[AnnuityInstallment]
    working_statements: list[WorkingStatementObligation]
    confidence: float
    confidence_band: ConfidenceBand
    rules_applied: list[str]
    rules_version: str
    warnings: list[str] = field(default_factory=list)

    @property
    def next_annuity(self) -> AnnuityInstallment | None:
        return next((a for a in self.annuities if a.outstanding), None)

    @property
    def next_working_statement(self) -> WorkingStatementObligation | None:
        return next((w for w in self.working_statements if w.status != ObligationStatus.SUBMITTED), None)

    @property
    def is_in_force(self) -> bool:
        return self.status in (LegalStatus.GRANTED, LegalStatus.LAPSE_SUSPECTED)


SOURCE_BASE_CONFIDENCE = {
    DataSource.MANUAL_VERIFIED: 0.95,
    DataSource.AGENT_FEED: 0.90,
    DataSource.PDKI: 0.85,
    DataSource.SAKI: 0.85,
    DataSource.USER_INPUT: 0.60,
    DataSource.SUPPLEMENTARY: 0.40,
}


def protection_end_date(filing_date: date, years: int, convention: TermEndConvention) -> date:
    end = add_years(filing_date, years)
    if convention == TermEndConvention.DAY_BEFORE_ANNIVERSARY:
        end -= timedelta(days=1)
    return end


def compute(facts: PatentFacts, registry: RuleRegistry, as_of: date) -> PatentComputation:
    if facts.grant_date and facts.grant_date < facts.filing_date:
        raise CalculationError("GRANT_BEFORE_FILING", "Tanggal pemberian sebelum tanggal penerimaan")
    if facts.paid_through_year is not None and facts.paid_through_year < 0:
        raise CalculationError("INVALID_PAID_THROUGH", "paid_through_year tidak valid")

    warnings: list[str] = []
    used: dict[str, RuleSet] = {}

    term_sel = registry.for_date(facts.filing_date)
    if term_sel.fallback:
        warnings.append("RULES_FALLBACK_PRE_2001")
    term_rules = term_sel.rules
    used[term_rules.id] = term_rules
    term_years = term_rules.terms[facts.patent_type].years
    end = protection_end_date(facts.filing_date, term_years, term_rules.term_end_convention)

    annuities = _annuity_schedule(facts, registry, term_years, end, as_of, warnings, used)
    if facts.paid_through_year is not None and facts.paid_through_year > term_years:
        warnings.append("PAID_THROUGH_BEYOND_TERM")
    working = _working_statements(facts, registry, end, annuities, as_of, used)
    status = _status(facts, end, annuities, as_of, warnings)

    if any(a.status == InstallmentStatus.UNKNOWN for a in annuities):
        warnings.append("PAYMENT_STATUS_UNKNOWN")
    if any(not rs.counsel_verified for rs in used.values()):
        warnings.append("RULES_NOT_COUNSEL_VERIFIED")
    if any(w.final_partial_year for w in working):
        warnings.append("WORKING_STATEMENT_FINAL_YEAR_VERIFY")

    confidence = _confidence(facts, as_of, warnings, term_sel.fallback)
    band = (
        ConfidenceBand.HIGH if confidence >= 0.8 else ConfidenceBand.MEDIUM if confidence >= 0.5 else ConfidenceBand.LOW
    )
    return PatentComputation(
        as_of=as_of,
        patent_type=facts.patent_type,
        term_years=term_years,
        protection_start=facts.filing_date,
        protection_end=end,
        status=status,
        annuities=annuities,
        working_statements=working,
        confidence=round(confidence, 2),
        confidence_band=band,
        rules_applied=sorted(used),
        rules_version=registry.version,
        warnings=list(dict.fromkeys(warnings)),
    )


def _installment_status(last_year: int, due: date, grace_end: date, paid_through: int | None, as_of: date) -> InstallmentStatus:
    if paid_through is not None and paid_through >= last_year:
        return InstallmentStatus.PAID
    if as_of <= due:
        return InstallmentStatus.UPCOMING
    if paid_through is None:
        return InstallmentStatus.UNKNOWN
    if as_of <= grace_end:
        return InstallmentStatus.IN_GRACE
    return InstallmentStatus.MISSED


def _annuity_schedule(
    facts: PatentFacts,
    registry: RuleRegistry,
    term_years: int,
    end: date,
    as_of: date,
    warnings: list[str],
    used: dict[str, RuleSet],
) -> list[AnnuityInstallment]:
    grant, filing = facts.grant_date, facts.filing_date
    if grant is None:
        return []
    if grant >= end:
        warnings.append("GRANT_AFTER_TERM_END")
        return []

    grant_year = anniversaries_passed(filing, grant) + 1
    first = registry.annuity_for_date(grant)
    used[first.rules.id] = first.rules
    first_last = min(grant_year + first.annuity.first_payment_extra_years_after_grant_year, term_years)

    # (first_year, last_year, due_date, timing_approximated)
    plan: list[tuple[int, int, date, bool]] = [
        (1, first_last, add_months(grant, first.annuity.first_payment_months_after_grant), first.approximated)
    ]
    for year in range(first_last + 1, term_years + 1):
        start = add_years(filing, year - 1)
        timing = registry.annuity_for_date(start)
        used[timing.rules.id] = timing.rules
        plan.append(
            (year, year, add_months(start, -timing.annuity.subsequent_months_before_anniversary), timing.approximated)
        )

    result = []
    for seq, (first_year, last_year, due, approximated) in enumerate(plan, start=1):
        grace = registry.annuity_for_date(due)
        used[grace.rules.id] = grace.rules
        grace_end = add_months(due, grace.annuity.grace_months)
        status = _installment_status(last_year, due, grace_end, facts.paid_through_year, as_of)
        if (approximated or grace.approximated) and status != InstallmentStatus.PAID:
            warnings.append("ANNUITY_RULES_APPROXIMATED")
        result.append(
            AnnuityInstallment(
                sequence=seq,
                first_year=first_year,
                last_year=last_year,
                coverage_start=add_years(filing, first_year - 1),
                coverage_end=min(add_years(filing, last_year) - timedelta(days=1), end),
                due_date=due,
                grace_end=grace_end,
                surcharge_percent=grace.annuity.grace_surcharge_percent,
                grace_requires_request=grace.annuity.grace_requires_request,
                status=status,
                rule_id=grace.rules.id,
            )
        )
    return result


def _ws_status(key: str, due: date, submitted: frozenset[str], as_of: date) -> ObligationStatus:
    if key in submitted:
        return ObligationStatus.SUBMITTED
    return ObligationStatus.UPCOMING if as_of <= due else ObligationStatus.OVERDUE


def _working_statements(
    facts: PatentFacts,
    registry: RuleRegistry,
    end: date,
    annuities: list[AnnuityInstallment],
    as_of: date,
    used: dict[str, RuleSet],
) -> list[WorkingStatementObligation]:
    grant = facts.grant_date
    if grant is None:
        return []
    out: list[WorkingStatementObligation] = []
    # Calendar-year mode
    for year in range(grant.year, end.year + 1):
        due = date(year, 12, 31)
        rs = registry.for_date(due).rules
        ws = rs.working_statement
        if not ws.enabled or ws.mode != WorkingStatementMode.CALENDAR_YEAR_END:
            continue
        if ws.applies_from_year is not None and year < ws.applies_from_year:
            continue
        if end <= date(year, 1, 1):
            continue
        used[rs.id] = rs
        key = str(year)
        out.append(
            WorkingStatementObligation(
                period_key=key,
                due_date=due,
                status=_ws_status(key, due, facts.working_statements_submitted, as_of),
                rule_id=rs.id,
                final_partial_year=end < due,
            )
        )
    # Annuity-aligned mode
    for inst in annuities:
        rs = registry.for_date(inst.due_date).rules
        ws = rs.working_statement
        if not ws.enabled or ws.mode != WorkingStatementMode.WITH_ANNUITY:
            continue
        if ws.applies_from_year is not None and inst.due_date.year < ws.applies_from_year:
            continue
        used[rs.id] = rs
        key = f"A{inst.sequence}"
        out.append(
            WorkingStatementObligation(
                period_key=key,
                due_date=inst.due_date,
                status=_ws_status(key, inst.due_date, facts.working_statements_submitted, as_of),
                rule_id=rs.id,
            )
        )
    out.sort(key=lambda w: w.due_date)
    return out


def _status(
    facts: PatentFacts, end: date, annuities: list[AnnuityInstallment], as_of: date, warnings: list[str]
) -> LegalStatus:
    recorded = facts.recorded_status
    if recorded in TERMINAL_RECORDED_STATUSES:
        return recorded
    if as_of > end:
        return LegalStatus.EXPIRED
    if recorded == LegalStatus.EXPIRED:
        warnings.append("RECORDED_EXPIRED_BEFORE_TERM_END")
        return LegalStatus.EXPIRED
    if facts.grant_date is None:
        if recorded == LegalStatus.GRANTED:
            warnings.append("GRANT_DATE_MISSING")
            return LegalStatus.GRANTED
        return LegalStatus.PENDING
    if recorded == LegalStatus.PENDING:
        warnings.append("RECORDED_PENDING_BUT_GRANT_DATE_SET")
    if any(a.status == InstallmentStatus.MISSED for a in annuities):
        return LegalStatus.LAPSE_SUSPECTED
    return LegalStatus.GRANTED


def _confidence(facts: PatentFacts, as_of: date, warnings: list[str], fallback: bool) -> float:
    score = SOURCE_BASE_CONFIDENCE[facts.source]
    if facts.last_verified_on is None:
        score -= 0.15
        warnings.append("NEVER_VERIFIED")
    else:
        age = (as_of - facts.last_verified_on).days
        if age > 365:
            score -= 0.20
            warnings.append("VERIFICATION_STALE")
        elif age > 180:
            score -= 0.10
            warnings.append("VERIFICATION_AGING")
        elif age > 90:
            score -= 0.05
    if fallback:
        score -= 0.20
    if "GRANT_DATE_MISSING" in warnings:
        score -= 0.20
    if "PAYMENT_STATUS_UNKNOWN" in warnings:
        score -= 0.10
    if "ANNUITY_RULES_APPROXIMATED" in warnings:
        score -= 0.05
    if "RECORDED_EXPIRED_BEFORE_TERM_END" in warnings or "RECORDED_PENDING_BUT_GRANT_DATE_SET" in warnings:
        score -= 0.10
    return max(0.0, min(1.0, score))
