from datetime import date

from idpatent.domain.alerts import StatusSnapshot, detect_changes, evaluate_alerts
from idpatent.domain.calculator import PatentFacts, compute
from idpatent.domain.enums import (
    AlertKind, ConfidenceBand, DataSource, LegalStatus, OwnerType, PatentType, Severity,
)
from idpatent.domain.matching import OwnerMatcher, normalize_owner_name, score_names
from idpatent.domain.rules import AlertPolicy, load_default_registry

REG = load_default_registry()


def test_normalize_indonesian_company():
    a = normalize_owner_name("PT. Telekomunikasi Indonesia Tbk")
    b = normalize_owner_name("P.T. TELEKOMUNIKASI INDONESIA (PERSERO), TBK")
    assert a.key == b.key == "TELEKOMUNIKASI INDONESIA"
    assert a.owner_type == OwnerType.COMPANY
    assert "PT" in a.legal_forms and "TBK" in a.legal_forms


def test_inventor_titles_stripped():
    n = normalize_owner_name("Prof. Dr. Ir. Budi Santoso, S.T., M.T.")
    assert n.key == "BUDI SANTOSO"
    assert n.owner_type == OwnerType.INDIVIDUAL


def test_university_alias_and_old_spelling():
    ugm = normalize_owner_name("UGM")
    old = normalize_owner_name("Universitas Gajah Mada")
    assert ugm.key == "UNIVERSITAS GADJAH MADA" and ugm.owner_type == OwnerType.UNIVERSITY
    score, reasons = score_names(ugm, old)
    assert score >= 92 and "SPELLING_VARIANT" in reasons


def test_foreign_company():
    n = normalize_owner_name("Samsung Electronics Co., Ltd.")
    assert n.owner_type == OwnerType.FOREIGN
    assert normalize_owner_name("Robert Bosch GmbH").owner_type == OwnerType.FOREIGN


def test_affiliate_not_auto_matched():
    parent = normalize_owner_name("PT Astra International Tbk")
    sub = normalize_owner_name("PT Astra International Otoparts")
    score, reasons = score_names(parent, sub)
    assert score < 92 and "SUBSET_NAME_POSSIBLE_AFFILIATE" in reasons


def test_legal_form_conflict_penalised():
    score, reasons = score_names(normalize_owner_name("CV Maju Jaya"), normalize_owner_name("PT Maju Jaya"))
    assert "LEGAL_FORM_CONFLICT" in reasons and score < 100


def test_matcher_ranks_candidates():
    m = OwnerMatcher()
    m.add(1, "Institut Teknologi Bandung")
    m.add(2, "PT Bio Farma (Persero)")
    m.add(3, "Universitas Indonesia")
    hits = m.match("ITB")
    assert hits[0].candidate_id == 1 and hits[0].band == ConfidenceBand.HIGH
    bio = m.match("PT. BIOFARMA")
    assert bio and bio[0].candidate_id == 2
    assert m.match("Toyota Jidosha Kabushiki Kaisha") == []


def _comp(as_of, **kw):
    facts = dict(
        patent_type=PatentType.PATEN, filing_date=date(2015, 3, 10), grant_date=date(2019, 8, 20),
        paid_through_year=12, source=DataSource.PDKI, last_verified_on=as_of,
        working_statements_submitted=frozenset({"2024", "2025"}),
    )
    facts.update(kw)
    return compute(PatentFacts(**facts), REG, as_of)


def test_alert_stages_and_dedupe():
    as_of = date(2026, 11, 22)
    alerts = {a.kind: a for a in evaluate_alerts(_comp(as_of), AlertPolicy(), as_of)}
    ann = alerts[AlertKind.ANNUITY_DUE]
    assert ann.stage == "90d" and ann.target_date == date(2027, 2, 10) and ann.severity == Severity.WARNING
    assert ann.dedupe_key == "ANNUITY_DUE:annuity:Y13:90d"
    ws = alerts[AlertKind.WORKING_STATEMENT_DUE]
    assert ws.stage == "60d" and ws.reference == "ws:2026"
    assert AlertKind.EXPIRY_APPROACHING not in alerts
    # ten days later the annuity enters the 60-day stage -> new dedupe key
    later = {a.kind: a for a in evaluate_alerts(_comp(date(2026, 12, 15)), AlertPolicy(), date(2026, 12, 15))}
    assert later[AlertKind.ANNUITY_DUE].stage == "60d"
    assert later[AlertKind.WORKING_STATEMENT_DUE].severity == Severity.CRITICAL


def test_expiry_alert_months():
    as_of = date(2034, 1, 1)
    alerts = evaluate_alerts(_comp(as_of, paid_through_year=20, working_statements_submitted=frozenset(str(y) for y in range(2024, 2036))), AlertPolicy(), as_of)
    exp = [a for a in alerts if a.kind == AlertKind.EXPIRY_APPROACHING][0]
    assert exp.stage == "18m" and exp.severity == Severity.INFO


def test_grace_and_lapse_alerts():
    as_of = date(2026, 5, 1)
    kinds = {a.kind for a in evaluate_alerts(_comp(as_of, paid_through_year=11), AlertPolicy(), as_of)}
    assert AlertKind.ANNUITY_GRACE in kinds
    as_of = date(2026, 11, 22)
    comp = _comp(as_of, paid_through_year=7)
    assert comp.status == LegalStatus.LAPSE_SUSPECTED
    lapse = [a for a in evaluate_alerts(comp, AlertPolicy(), as_of) if a.kind == AlertKind.LAPSE_RISK]
    assert lapse and all(a.severity == Severity.CRITICAL for a in lapse)


def test_unverified_payment_alert():
    as_of = date(2026, 3, 1)  # year-12 installment due 2026-02-10, payment status unknown
    alerts = evaluate_alerts(_comp(as_of, paid_through_year=None), AlertPolicy(), as_of)
    assert any(a.kind == AlertKind.ANNUITY_PAYMENT_UNVERIFIED and a.reference == "annuity:Y12" for a in alerts)


def test_quiet_for_inactive_patents():
    as_of = date(2026, 11, 22)
    assert evaluate_alerts(_comp(as_of, recorded_status=LegalStatus.WITHDRAWN), AlertPolicy(), as_of) == []


def test_status_and_owner_change_detection():
    as_of = date(2026, 11, 22)
    changes = detect_changes(StatusSnapshot(LegalStatus.GRANTED, "o1"), StatusSnapshot(LegalStatus.LAPSE_SUSPECTED, "o2"), as_of)
    kinds = {c.kind for c in changes}
    assert kinds == {AlertKind.LAPSE_RISK, AlertKind.OWNERSHIP_CHANGE}
    assert detect_changes(None, StatusSnapshot(LegalStatus.GRANTED), as_of) == []


def test_alert_policy_validation():
    p = AlertPolicy.from_dict({"annuity_days": [30, 7, 30]})
    assert p.annuity_days == (30, 7)
    try:
        AlertPolicy.from_dict({"annuity_days": [-1]})
    except ValueError:
        pass
    else:
        raise AssertionError("negative lead time accepted")
