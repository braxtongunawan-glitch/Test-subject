"""Dashboard metrics and localized exports (SRS 3.5)."""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Alert, Owner, Patent
from ..domain.dates import format_date
from ..domain.enums import (
    INACTIVE_STATUSES, ConfidenceBand, DataSource, LegalStatus, OwnerType, PatentType,
)
from ..domain.i18n import label


def _status(p: Patent) -> LegalStatus | None:
    return LegalStatus(p.computed_status) if p.computed_status else None


def dashboard(session: Session, firm_id: int, as_of: date, lang: str = "id", owner_ids: list[int] | None = None) -> dict:
    q = select(Patent).where(Patent.firm_id == firm_id)
    if owner_ids is not None:
        q = q.where(Patent.owner_id.in_(owner_ids))
    patents = list(session.execute(q).scalars())
    owners = {o.id: o for o in session.execute(select(Owner).where(Owner.firm_id == firm_id)).scalars()}
    active = [p for p in patents if _status(p) not in INACTIVE_STATUSES and _status(p) != LegalStatus.PENDING]

    def window(attr: str, days: int) -> int:
        until = as_of + timedelta(days=days)
        return sum(1 for p in active if getattr(p, attr) and as_of <= getattr(p, attr) <= until)

    by_type = Counter(p.patent_type for p in patents)
    by_status = Counter(p.computed_status for p in patents)
    by_owner_type = Counter(owners[p.owner_id].owner_type if p.owner_id in owners else OwnerType.UNKNOWN.value for p in patents)
    by_confidence = Counter(p.confidence_band for p in patents)
    stale = sum(1 for p in active if not p.last_verified_on or (as_of - p.last_verified_on).days > 180)
    ws_overdue = sum(1 for p in active if p.next_ws_due and p.next_ws_due < as_of)
    grace = sum(1 for p in active if p.next_annuity_due and p.next_annuity_due < as_of <= (p.next_grace_end or as_of))
    alerts_q = select(Alert).where(Alert.firm_id == firm_id, Alert.status == "OPEN")
    open_alerts = list(session.execute(alerts_q).scalars())
    if owner_ids is not None:
        allowed = {p.id for p in patents}
        open_alerts = [a for a in open_alerts if a.patent_id in allowed]

    def labelled(counter: Counter, enum) -> list[dict]:
        return [
            {"code": k, "label": label(enum(k), lang) if k else "-", "count": v}
            for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
        ]

    return {
        "as_of": as_of.isoformat(),
        "as_of_text": format_date(as_of, lang),
        "totals": {"patents": len(patents), "active": len(active), "owners": len({p.owner_id for p in patents if p.owner_id})},
        "by_type": labelled(by_type, PatentType),
        "by_status": labelled(by_status, LegalStatus),
        "by_owner_type": labelled(by_owner_type, OwnerType),
        "by_confidence": labelled(by_confidence, ConfidenceBand),
        "annuity_pipeline": {"30": window("next_annuity_due", 30), "90": window("next_annuity_due", 90),
                             "180": window("next_annuity_due", 180), "in_grace": grace},
        "working_statement": {"due_90": window("next_ws_due", 90), "overdue": ws_overdue},
        "expiring_12_months": window("protection_end", 365),
        "lapse_suspected": by_status.get(LegalStatus.LAPSE_SUSPECTED.value, 0),
        "data_freshness": {"stale_or_never_verified": stale},
        "open_alerts": dict(Counter(a.severity for a in open_alerts)),
    }


_HEADERS = {
    "id": ["Nomor Permohonan", "Nomor Paten", "Jenis", "Judul", "Pemegang", "Jenis Pemegang", "Tanggal Penerimaan",
           "Tanggal Pemberian", "Status", "Pelindungan Berakhir", "Biaya Tahunan Berikutnya", "Akhir Masa Tenggang",
           "Pernyataan Pelaksanaan Berikutnya", "Keyakinan", "Sumber", "Terakhir Diverifikasi"],
    "en": ["Application No.", "Patent No.", "Type", "Title", "Owner", "Owner Type", "Filing Date", "Grant Date",
           "Status", "Protection Ends", "Next Annuity Due", "Grace Period Ends", "Next Working Statement",
           "Confidence", "Source", "Last Verified"],
}


def portfolio_csv(session: Session, firm_id: int, lang: str = "id", owner_ids: list[int] | None = None) -> str:
    q = select(Patent).where(Patent.firm_id == firm_id).order_by(Patent.protection_end)
    if owner_ids is not None:
        q = q.where(Patent.owner_id.in_(owner_ids))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";" if lang == "id" else ",")
    w.writerow(_HEADERS[lang])
    for p in session.execute(q).scalars():
        owner = p.owner
        w.writerow([
            p.application_number or "", p.registration_number or "", label(PatentType(p.patent_type), lang),
            _safe(p.title), _safe(owner.name) if owner else "", label(OwnerType(owner.owner_type), lang) if owner else "",
            format_date(p.filing_date, lang), format_date(p.grant_date, lang),
            label(LegalStatus(p.computed_status), lang) if p.computed_status else "",
            format_date(p.protection_end, lang), format_date(p.next_annuity_due, lang),
            format_date(p.next_grace_end, lang), format_date(p.next_ws_due, lang),
            label(ConfidenceBand(p.confidence_band), lang) if p.confidence_band else "",
            label(DataSource(p.source), lang), format_date(p.last_verified_on, lang),
        ])
    return buf.getvalue()


def _safe(text: str | None) -> str:
    """Neutralise spreadsheet formula injection in exported text cells."""
    if not text:
        return ""
    return "'" + text if text[0] in "=+-@\t\r" else text
