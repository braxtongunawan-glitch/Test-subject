"""Bulk import and the scheduled alert/recompute run."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import Firm, Patent
from ..domain.enums import DataSource
from ..ingestion.parsers import ParseError, read_rows, rows_to_records
from . import audit
from .audit import Actor
from .context import AppContext
from .patents import OwnerResolver, PatentInputError, recompute, upsert_patent


def import_file(session: Session, ctx: AppContext, firm: Firm, filename: str, content: bytes, source: DataSource,
                actor: Actor, as_of: date) -> dict:
    try:
        rows = read_rows(filename, content)
    except (ParseError, ValueError) as exc:
        raise PatentInputError("UNREADABLE_FILE", str(exc)) from exc
    records, row_errors = rows_to_records(rows, source)
    errors = [asdict(e) for e in row_errors]
    owners = OwnerResolver(session, firm.id)
    created = updated = 0
    warnings: list[dict] = []
    review: list[int] = []
    alerts = 0
    for rec in records:
        try:
            with session.begin_nested():
                result = upsert_patent(session, ctx, firm, rec, actor, as_of, owners=owners)
        except PatentInputError as exc:
            errors.append({"row": rec.row_number, "field": exc.code, "message": str(exc)})
            owners = OwnerResolver(session, firm.id)  # drop owners created inside the rolled-back savepoint
            continue
        except IntegrityError:
            errors.append({"row": rec.row_number, "field": "INTEGRITY", "message": "Data bertentangan dengan data yang ada"})
            owners = OwnerResolver(session, firm.id)
            continue
        created += result.created
        updated += not result.created
        alerts += len(result.new_alerts)
        if result.warnings:
            warnings.append({"row": rec.row_number, "patent_id": result.patent.id, "codes": result.warnings})
        if result.owner_needs_review:
            review.append(result.patent.id)
    summary = {
        "rows": len(rows), "created": created, "updated": updated, "errors": errors, "warnings": warnings,
        "owner_review_patent_ids": review, "new_alerts": alerts,
    }
    audit.record(session, actor, "patent.import", "file", filename,
                 {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()}, firm.id)
    return summary


def run_daily(session: Session, ctx: AppContext, firm: Firm, as_of: date, actor: Actor | None = None) -> dict:
    """Recompute every patent of a firm and persist newly reached alert stages."""
    actor = actor or Actor.system("scheduler", firm.id)
    patents = session.execute(select(Patent).where(Patent.firm_id == firm.id)).scalars().all()
    new_alerts = 0
    by_kind: dict[str, int] = {}
    for p in patents:
        _, created = recompute(session, ctx, firm, p, as_of)
        new_alerts += len(created)
        for a in created:
            by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
    audit.record(session, actor, "alerts.run", "firm", firm.id,
                 {"as_of": as_of.isoformat(), "patents": len(patents), "new_alerts": new_alerts}, firm.id)
    return {"patents": len(patents), "new_alerts": new_alerts, "by_kind": by_kind}
