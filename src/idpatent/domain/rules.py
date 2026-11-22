"""Effective-dated, data-driven legal rule sets.

Legal parameters (term lengths, annuity timing, grace periods, Working
Statement obligations) are stored as JSON documents in ``idpatent/rules`` rather
than in code, so that a regulation change is a reviewed data change. Each rule
set carries its own verification status, which is surfaced on every
computation until Indonesian counsel signs it off.

Selection: the rule set in force on the *relevant date* applies – the filing
date for the protection term, the grant date for the first annuity, and each
installment's due date for its grace period.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path

from .enums import PatentType, TermEndConvention, WorkingStatementMode


@dataclass(frozen=True)
class TermRule:
    years: int
    citation: str


@dataclass(frozen=True)
class AnnuityRule:
    first_payment_months_after_grant: int
    first_payment_extra_years_after_grant_year: int
    subsequent_months_before_anniversary: int
    grace_months: int
    grace_surcharge_percent: int
    grace_requires_request: bool
    citation: str


@dataclass(frozen=True)
class WorkingStatementRule:
    enabled: bool
    mode: WorkingStatementMode
    applies_from_year: int | None
    citation: str | None
    note: str | None


@dataclass(frozen=True)
class RuleSet:
    id: str
    title_id: str
    title_en: str
    effective_from: date
    legal_basis: tuple[str, ...]
    verification_status: str
    verification_note: str
    term_end_convention: TermEndConvention
    terms: dict[PatentType, TermRule]
    annuity: AnnuityRule | None  # None: annuity timing not modelled for this era
    working_statement: WorkingStatementRule
    content_hash: str

    @property
    def counsel_verified(self) -> bool:
        return self.verification_status == "VERIFIED"

    @classmethod
    def from_dict(cls, data: dict) -> "RuleSet":
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ws = data["working_statement"]
        ann = data["annuity"]
        return cls(
            id=data["id"],
            title_id=data["title"]["id"],
            title_en=data["title"]["en"],
            effective_from=date.fromisoformat(data["effective_from"]),
            legal_basis=tuple(data.get("legal_basis", [])),
            verification_status=data["verification"]["status"],
            verification_note=data["verification"].get("note") or "",
            term_end_convention=TermEndConvention(data["term_end_convention"]),
            terms={PatentType(k): TermRule(v["years"], v["citation"]) for k, v in data["terms"].items()},
            annuity=None if ann is None else AnnuityRule(
                first_payment_months_after_grant=ann["first_payment_months_after_grant"],
                first_payment_extra_years_after_grant_year=ann["first_payment_extra_years_after_grant_year"],
                subsequent_months_before_anniversary=ann["subsequent_months_before_anniversary"],
                grace_months=ann["grace_months"],
                grace_surcharge_percent=ann["grace_surcharge_percent"],
                grace_requires_request=ann["grace_requires_request"],
                citation=ann["citation"],
            ),
            working_statement=WorkingStatementRule(
                enabled=ws["enabled"],
                mode=WorkingStatementMode(ws["mode"]),
                applies_from_year=ws.get("applies_from_year"),
                citation=ws.get("citation"),
                note=ws.get("note"),
            ),
            content_hash=hashlib.sha256(canonical).hexdigest(),
        )


@dataclass(frozen=True)
class RuleSelection:
    rules: RuleSet
    fallback: bool  # True when the date predates every known rule set


@dataclass(frozen=True)
class AnnuitySelection:
    rules: RuleSet  # the rule set whose annuity parameters were used
    annuity: AnnuityRule
    approximated: bool  # True when the era's own annuity rules are not modelled


class RuleRegistry:
    def __init__(self, rule_sets: list[RuleSet]):
        if not rule_sets:
            raise ValueError("At least one rule set is required")
        self._sets = sorted(rule_sets, key=lambda r: r.effective_from)
        ids = [r.id for r in self._sets]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate rule set ids")

    @property
    def rule_sets(self) -> list[RuleSet]:
        return list(self._sets)

    @property
    def version(self) -> str:
        """Stable fingerprint of all rule content, stored with computations for audit."""
        joined = "|".join(f"{r.id}:{r.content_hash}" for r in self._sets)
        return hashlib.sha256(joined.encode()).hexdigest()[:16]

    def for_date(self, on: date) -> RuleSelection:
        current = None
        for rs in self._sets:
            if rs.effective_from <= on:
                current = rs
        if current is None:
            return RuleSelection(self._sets[0], fallback=True)
        return RuleSelection(current, fallback=False)

    def annuity_for_date(self, on: date) -> AnnuitySelection:
        sel = self.for_date(on)
        if sel.rules.annuity is not None:
            return AnnuitySelection(sel.rules, sel.rules.annuity, approximated=sel.fallback)
        later = [r for r in self._sets if r.annuity is not None and r.effective_from > sel.rules.effective_from]
        if not later:
            raise ValueError("No rule set defines annuity parameters")
        return AnnuitySelection(later[0], later[0].annuity, approximated=True)

    @classmethod
    def from_directory(cls, path: Path) -> "RuleRegistry":
        sets = [
            RuleSet.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(path.glob("id_*.json"))
        ]
        return cls(sets)


def load_default_registry() -> RuleRegistry:
    pkg = resources.files("idpatent.rules")
    sets = []
    for entry in sorted(pkg.iterdir(), key=lambda e: e.name):
        if entry.name.startswith("id_") and entry.name.endswith(".json"):
            sets.append(RuleSet.from_dict(json.loads(entry.read_text(encoding="utf-8"))))
    return RuleRegistry(sets)


@dataclass(frozen=True)
class AlertPolicy:
    """Firm-configurable lead times (not legal rules)."""

    expiry_months: tuple[int, ...] = (18, 12, 6, 3, 1)
    annuity_days: tuple[int, ...] = (90, 60, 30, 14, 7)
    grace_end_days: tuple[int, ...] = (90, 30, 14, 7)
    working_statement_days: tuple[int, ...] = (60, 30, 14, 7)
    critical_within_days: int = 30
    warning_within_days: int = 90

    @classmethod
    def from_dict(cls, data: dict | None) -> "AlertPolicy":
        if not data:
            return cls()
        base = cls()
        def tup(key: str) -> tuple[int, ...]:
            vals = data.get(key, getattr(base, key))
            if any((not isinstance(v, int)) or v <= 0 for v in vals):
                raise ValueError(f"{key} must contain positive integers")
            return tuple(sorted(set(vals), reverse=True))
        return cls(
            expiry_months=tup("expiry_months"),
            annuity_days=tup("annuity_days"),
            grace_end_days=tup("grace_end_days"),
            working_statement_days=tup("working_statement_days"),
            critical_within_days=int(data.get("critical_within_days", base.critical_within_days)),
            warning_within_days=int(data.get("warning_within_days", base.warning_within_days)),
        )

    def to_dict(self) -> dict:
        return {
            "expiry_months": list(self.expiry_months),
            "annuity_days": list(self.annuity_days),
            "grace_end_days": list(self.grace_end_days),
            "working_statement_days": list(self.working_statement_days),
            "critical_within_days": self.critical_within_days,
            "warning_within_days": self.warning_within_days,
        }


def load_default_alert_policy() -> AlertPolicy:
    text = resources.files("idpatent.rules").joinpath("alert_policy_default.json").read_text(encoding="utf-8")
    return AlertPolicy.from_dict(json.loads(text))
