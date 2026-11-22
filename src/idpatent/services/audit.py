"""Hash-chained audit logging of data access, changes and outbound communications."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..compliance.audit_chain import GENESIS_HASH, ChainVerification, chain_hash, verify_chain
from ..db.models import AuditLog, utcnow


class Actor:
    """Who performed an action: a user, or a system process."""

    def __init__(self, user_id: int | None, label: str, firm_id: int | None = None, ip: str | None = None):
        self.user_id, self.label, self.firm_id, self.ip = user_id, label, firm_id, ip

    @classmethod
    def system(cls, label: str = "system", firm_id: int | None = None) -> "Actor":
        return cls(None, label, firm_id)


def _lock(session: Session, key: int) -> None:
    # Serialize chain appends. SQLite already serializes writers; PostgreSQL needs a lock.
    if session.get_bind().dialect.name == "postgresql":  # pragma: no cover
        from sqlalchemy import text

        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def _payload(row: AuditLog) -> dict:
    return {
        "occurred_at": row.occurred_at.isoformat(),
        "firm_id": row.firm_id,
        "actor_user_id": row.actor_user_id,
        "actor_label": row.actor_label,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "details": row.details,
        "ip": row.ip,
    }


def record(
    session: Session,
    actor: Actor,
    action: str,
    entity_type: str | None = None,
    entity_id: object | None = None,
    details: dict | None = None,
    firm_id: int | None = None,
) -> AuditLog:
    _lock(session, 0x1D9A7E01)
    last = session.execute(select(AuditLog.entry_hash).order_by(AuditLog.id.desc()).limit(1)).scalar()
    prev = last or GENESIS_HASH
    row = AuditLog(
        occurred_at=utcnow().replace(microsecond=0),
        firm_id=firm_id if firm_id is not None else actor.firm_id,
        actor_user_id=actor.user_id,
        actor_label=actor.label,
        action=action,
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        details=details or {},
        ip=actor.ip,
        prev_hash=prev,
        entry_hash="",
    )
    row.entry_hash = chain_hash(prev, _payload(row))
    session.add(row)
    session.flush()
    return row


def verify(session: Session) -> ChainVerification:
    rows = session.execute(select(AuditLog).order_by(AuditLog.id)).scalars()
    return verify_chain((r.id, _payload(r), r.prev_hash, r.entry_hash) for r in rows)
