"""Localized read models shared by the API, client portal, reports and e-mails."""

from __future__ import annotations

from ..db.models import Alert, Patent
from ..domain.calculator import AnnuityInstallment, PatentComputation
from ..domain.dates import format_date, format_datetime_wib
from ..domain.enums import (
    AlertKind, ConfidenceBand, DataSource, InstallmentStatus, LegalStatus, ObligationStatus, OwnerType, PatentType,
    Severity,
)
from ..domain.i18n import alert_title, label, warning_text


def annuity_years_text(inst: AnnuityInstallment, lang: str) -> str:
    if inst.first_year == inst.last_year:
        return f"tahun ke-{inst.last_year}" if lang == "id" else f"year {inst.last_year}"
    return (
        f"tahun ke-{inst.first_year} s.d. ke-{inst.last_year}" if lang == "id" else f"years {inst.first_year}-{inst.last_year}"
    )


def _d(value, lang):
    return {"iso": value.isoformat(), "text": format_date(value, lang)} if value else None


def patent_summary(p: Patent, lang: str = "id") -> dict:
    return {
        "id": p.id,
        "number": p.display_number,
        "application_number": p.application_number,
        "registration_number": p.registration_number,
        "patent_type": p.patent_type,
        "patent_type_label": label(PatentType(p.patent_type), lang),
        "title": p.title,
        "owner": {"id": p.owner.id, "name": p.owner.name, "type": p.owner.owner_type} if p.owner else None,
        "status": p.computed_status,
        "status_label": label(LegalStatus(p.computed_status), lang) if p.computed_status else None,
        "protection_end": _d(p.protection_end, lang),
        "next_annuity_due": _d(p.next_annuity_due, lang),
        "next_grace_end": _d(p.next_grace_end, lang),
        "next_working_statement_due": _d(p.next_ws_due, lang),
        "confidence": p.confidence,
        "confidence_band": p.confidence_band,
        "confidence_label": label(ConfidenceBand(p.confidence_band), lang) if p.confidence_band else None,
        "source": p.source,
        "source_label": label(DataSource(p.source), lang),
        "last_verified_on": _d(p.last_verified_on, lang),
        "verification_status": p.verification_status,
    }


def patent_detail(p: Patent, comp: PatentComputation, lang: str = "id") -> dict:
    data = patent_summary(p, lang)
    data.update(
        {
            "filing_date": _d(p.filing_date, lang),
            "grant_date": _d(p.grant_date, lang),
            "recorded_status": p.recorded_status,
            "is_pct_national_phase": p.is_pct_national_phase,
            "pct_application_number": p.pct_application_number,
            "pct_publication_number": p.pct_publication_number,
            "priority_claims": p.priority_claims,
            "family_id": p.family_id,
            "agent_name": p.agent_name,
            "term_years": comp.term_years,
            "annuities": [
                {
                    "sequence": a.sequence,
                    "years": [a.first_year, a.last_year],
                    "years_text": annuity_years_text(a, lang),
                    "coverage_start": _d(a.coverage_start, lang),
                    "coverage_end": _d(a.coverage_end, lang),
                    "due_date": _d(a.due_date, lang),
                    "grace_end": _d(a.grace_end, lang),
                    "surcharge_percent": a.surcharge_percent,
                    "grace_requires_request": a.grace_requires_request,
                    "status": a.status.value,
                    "status_label": label(a.status, lang),
                    "rule_id": a.rule_id,
                }
                for a in comp.annuities
            ],
            "working_statements": [
                {
                    "period": w.period_key,
                    "due_date": _d(w.due_date, lang),
                    "status": w.status.value,
                    "status_label": label(w.status, lang),
                    "final_partial_year": w.final_partial_year,
                    "rule_id": w.rule_id,
                }
                for w in comp.working_statements
            ],
            "payments": [
                {"id": x.id, "years": [x.first_year, x.last_year], "paid_on": x.paid_on.isoformat(),
                 "amount_idr": x.amount_idr, "reference": x.reference, "source": x.source}
                for x in p.payments
            ],
            "working_statement_filings": [
                {"period": f.period_key, "submitted_on": f.submitted_on.isoformat(), "declaration": f.declaration,
                 "reference": f.reference}
                for f in p.ws_filings
            ],
            "provenance": {
                "source": p.source,
                "source_label": label(DataSource(p.source), lang),
                "source_reference": p.source_reference,
                "last_verified_on": _d(p.last_verified_on, lang),
                "verification_status": p.verification_status,
                "overrides": p.overrides,
                "computed_at": format_datetime_wib(p.computed_at, lang) if p.computed_at else None,
            },
            "warnings": [{"code": w, "text": warning_text(w, lang)} for w in comp.warnings],
            "rules": {"applied": comp.rules_applied, "version": comp.rules_version},
        }
    )
    return data


def alert_view(a: Alert, lang: str = "id") -> dict:
    number = a.patent.display_number if a.patent else str(a.patent_id)
    return {
        "id": a.id,
        "patent_id": a.patent_id,
        "patent_number": number,
        "kind": a.kind,
        "title": alert_title(AlertKind(a.kind), number, format_date(a.target_date, lang), lang),
        "reference": a.reference,
        "stage": a.stage,
        "target_date": a.target_date.isoformat(),
        "trigger_date": a.trigger_date.isoformat(),
        "severity": a.severity,
        "severity_label": label(Severity(a.severity), lang),
        "status": a.status,
        "created_at": format_datetime_wib(a.created_at, lang),
    }


def owner_type_label(value: str, lang: str) -> str:
    return label(OwnerType(value), lang)


def email_patent_context(p: Patent, comp: PatentComputation, lang: str) -> dict:
    nxt = comp.next_annuity
    nws = comp.next_working_statement
    return {
        "number": p.display_number,
        "type_label": label(PatentType(p.patent_type), lang),
        "title": p.title,
        "filing_date": format_date(p.filing_date, lang),
        "protection_end": format_date(comp.protection_end, lang),
        "status_label": label(comp.status, lang),
        "annuity_years": annuity_years_text(nxt, lang) if nxt else None,
        "annuity_due": format_date(nxt.due_date, lang) if nxt else None,
        "grace_end": format_date(nxt.grace_end, lang) if nxt else None,
        "surcharge_percent": nxt.surcharge_percent if nxt else None,
        "ws_period": nws.period_key if nws else None,
        "ws_due": format_date(nws.due_date, lang) if nws else None,
    }


__all__ = [
    "annuity_years_text", "patent_summary", "patent_detail", "alert_view", "email_patent_context",
    "InstallmentStatus", "ObligationStatus",
]
