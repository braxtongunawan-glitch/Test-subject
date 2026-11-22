from dataclasses import replace
from datetime import date

import pytest

from idpatent.domain.calculator import CalculationError, PatentFacts, compute
from idpatent.domain.enums import (
    ConfidenceBand, DataSource, InstallmentStatus, LegalStatus, ObligationStatus, PatentType,
    TermEndConvention, WorkingStatementMode,
)
from idpatent.domain.rules import RuleRegistry, load_default_registry

REG = load_default_registry()
AS_OF = date(2026, 11, 22)


def base_facts(**kw):
    defaults = dict(
        patent_type=PatentType.PATEN,
        filing_date=date(2015, 3, 10),
        grant_date=date(2019, 8, 20),
        paid_through_year=12,
        source=DataSource.PDKI,
        last_verified_on=date(2026, 10, 1),
        working_statements_submitted=frozenset({"2024", "2025"}),
    )
    defaults.update(kw)
    return PatentFacts(**defaults)


def test_registry_selection():
    assert REG.for_date(date(2010, 1, 1)).rules.id == "ID-UU14-2001"
    assert REG.for_date(date(2020, 1, 1)).rules.id == "ID-UU13-2016"
    assert REG.for_date(date(2024, 10, 28)).rules.id == "ID-UU65-2024"
    sel = REG.for_date(date(1999, 1, 1))
    assert sel.fallback and sel.rules.id == "ID-UU14-2001"
    approx = REG.annuity_for_date(date(2010, 1, 1))
    assert approx.approximated and approx.rules.id == "ID-UU13-2016"


def test_standard_patent_schedule():
    c = compute(base_facts(), REG, AS_OF)
    assert c.term_years == 20
    assert c.protection_end == date(2035, 3, 10)
    assert c.status == LegalStatus.GRANTED
    first = c.annuities[0]
    # grant on 2019-08-20 falls in protection year 5 -> first payment covers years 1..6
    assert (first.first_year, first.last_year) == (1, 6)
    assert first.due_date == date(2020, 2, 20)
    # due under UU 13/2016: 12-month grace on request
    assert first.grace_end == date(2021, 2, 20) and first.grace_requires_request
    assert len(c.annuities) == 15
    assert [a.last_year for a in c.annuities[1:3]] == [7, 8]
    assert c.annuities[1].due_date == date(2021, 2, 10)  # one month before 2021-03-10
    nxt = c.next_annuity
    assert nxt.first_year == 13 and nxt.due_date == date(2027, 2, 10)
    assert nxt.status == InstallmentStatus.UPCOMING
    # due after UU 65/2024: automatic 6-month grace, 100% surcharge
    assert nxt.grace_end == date(2027, 8, 10) and not nxt.grace_requires_request
    assert nxt.surcharge_percent == 100
    assert c.annuities[-1].first_year == 20
    assert c.annuities[-1].coverage_end == date(2035, 3, 9)
    assert set(c.rules_applied) == {"ID-UU14-2001", "ID-UU13-2016", "ID-UU65-2024"}
    assert "RULES_FALLBACK_PRE_2001" not in c.warnings


def test_working_statements_calendar_mode():
    c = compute(base_facts(), REG, AS_OF)
    keys = [w.period_key for w in c.working_statements]
    assert keys == [str(y) for y in range(2024, 2036)]
    assert c.working_statements[0].status == ObligationStatus.SUBMITTED
    nws = c.next_working_statement
    assert nws.period_key == "2026" and nws.due_date == date(2026, 12, 31)
    assert nws.status == ObligationStatus.UPCOMING
    assert c.working_statements[-1].final_partial_year
    assert "WORKING_STATEMENT_FINAL_YEAR_VERIFY" in c.warnings


def test_overdue_working_statement():
    c = compute(base_facts(working_statements_submitted=frozenset()), REG, AS_OF)
    assert c.working_statements[0].status == ObligationStatus.OVERDUE


def test_simple_patent_schedule():
    facts = base_facts(
        patent_type=PatentType.PATEN_SEDERHANA, filing_date=date(2020, 1, 15), grant_date=date(2021, 6, 1),
        paid_through_year=None,
    )
    c = compute(facts, REG, date(2022, 11, 1))
    assert c.protection_end == date(2030, 1, 15)
    assert (c.annuities[0].first_year, c.annuities[0].last_year) == (1, 3)
    assert c.annuities[0].due_date == date(2021, 12, 1)
    assert c.annuities[0].status == InstallmentStatus.UNKNOWN
    assert len(c.annuities) == 8
    assert c.annuities[1].due_date == date(2022, 12, 15)
    assert c.next_annuity.sequence == 2
    assert "PAYMENT_STATUS_UNKNOWN" in c.warnings


def test_leap_day_filing():
    facts = base_facts(patent_type=PatentType.PATEN_SEDERHANA, filing_date=date(2016, 2, 29), grant_date=None)
    c = compute(facts, REG, AS_OF)
    assert c.protection_end == date(2026, 2, 28)
    assert c.status == LegalStatus.EXPIRED


def test_pending_application():
    c = compute(base_facts(filing_date=date(2024, 5, 1), grant_date=None, paid_through_year=None), REG, AS_OF)
    assert c.status == LegalStatus.PENDING
    assert c.annuities == [] and c.working_statements == []
    assert c.protection_end == date(2044, 5, 1)


def test_missed_annuity_flags_lapse_risk():
    c = compute(base_facts(paid_through_year=7), REG, AS_OF)
    assert c.annuities[2].status == InstallmentStatus.MISSED
    assert c.status == LegalStatus.LAPSE_SUSPECTED
    assert c.next_annuity.sequence == 3


def test_in_grace_period():
    # year 12 due 2026-02-10 under UU 65/2024 -> grace until 2026-08-10
    c = compute(base_facts(paid_through_year=11), REG, date(2026, 5, 1))
    inst = c.next_annuity
    assert inst.last_year == 12 and inst.status == InstallmentStatus.IN_GRACE
    assert inst.grace_end == date(2026, 8, 10)
    assert c.status == LegalStatus.GRANTED


def test_recorded_terminal_status_wins():
    c = compute(base_facts(recorded_status=LegalStatus.LAPSED), REG, AS_OF)
    assert c.status == LegalStatus.LAPSED


def test_pre_2016_grant_with_unpaid_history_is_approximated():
    facts = base_facts(filing_date=date(2008, 4, 1), grant_date=date(2012, 1, 5), paid_through_year=None)
    c = compute(facts, REG, AS_OF)
    assert c.protection_end == date(2028, 4, 1)
    assert "ANNUITY_RULES_APPROXIMATED" in c.warnings
    assert c.confidence < compute(base_facts(), REG, AS_OF).confidence


def test_pre_2001_filing_uses_fallback():
    c = compute(base_facts(filing_date=date(1998, 4, 1), grant_date=date(2001, 1, 5)), REG, AS_OF)
    assert "RULES_FALLBACK_PRE_2001" in c.warnings
    assert c.status == LegalStatus.EXPIRED


def test_confidence_bands():
    high = compute(base_facts(source=DataSource.MANUAL_VERIFIED), REG, AS_OF)
    assert high.confidence_band == ConfidenceBand.HIGH
    low = compute(base_facts(source=DataSource.SUPPLEMENTARY, last_verified_on=None), REG, AS_OF)
    assert low.confidence_band == ConfidenceBand.LOW
    assert "NEVER_VERIFIED" in low.warnings
    assert "RULES_NOT_COUNSEL_VERIFIED" in high.warnings


def test_grant_before_filing_rejected():
    with pytest.raises(CalculationError):
        compute(base_facts(grant_date=date(2014, 1, 1)), REG, AS_OF)


def _variant_registry(**changes):
    sets = []
    for rs in REG.rule_sets:
        if rs.id == "ID-UU65-2024":
            ws = replace(rs.working_statement, **changes.pop("ws", {}))
            rs = replace(rs, working_statement=ws, **changes)
        sets.append(rs)
    return RuleRegistry(sets)


def test_with_annuity_mode_is_configurable():
    reg = _variant_registry(ws={"mode": WorkingStatementMode.WITH_ANNUITY})
    c = compute(base_facts(working_statements_submitted=frozenset()), reg, AS_OF)
    assert all(w.period_key.startswith("A") for w in c.working_statements)
    first = c.working_statements[0]
    assert first.due_date == date(2025, 2, 10)  # installment for year 11, due after the 2024 law


def test_day_before_convention():
    reg = RuleRegistry([replace(rs, term_end_convention=TermEndConvention.DAY_BEFORE_ANNIVERSARY) for rs in REG.rule_sets])
    c = compute(base_facts(), reg, AS_OF)
    assert c.protection_end == date(2035, 3, 9)
