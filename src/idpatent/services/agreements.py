"""Versioned click-wrap agreements and immutable acceptance records (SRS section 11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from importlib import resources

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..compliance.audit_chain import GENESIS_HASH, chain_hash, verify_chain
from ..compliance.clickwrap import (
    CHECKBOX_TEXT, AcceptanceInfo, VersionInfo, document_hash, pending_kinds, required_kinds,
)
from ..db.models import AgreementAcceptance, AgreementDocument, Firm, User, utcnow
from ..domain.dates import format_datetime_wib
from ..domain.enums import AgreementKind, Role
from . import audit
from .audit import Actor

_DEFAULT_FILES = {
    AgreementKind.TERMS_OF_SERVICE: "terms_of_service",
    AgreementKind.PRIVACY_POLICY: "privacy_policy",
    AgreementKind.DATA_PROCESSING_NOTICE: "data_processing_notice",
    AgreementKind.MARKETING_CONSENT: "marketing_consent",
}


class AgreementError(ValueError):
    def __init__(self, code: str, message: str, details: list | None = None):
        super().__init__(message)
        self.code, self.details = code, details or []


def _title_and_body(text: str) -> tuple[str, str]:
    lines = text.strip().splitlines()
    title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else "Dokumen"
    return title, text.strip()


def publish_version(session: Session, kind: AgreementKind, version: str, texts: dict[str, str], *, material: bool,
                    effective_at: datetime, counsel_reviewed: bool, actor: Actor, production: bool) -> list[AgreementDocument]:
    if production and not counsel_reviewed:
        raise AgreementError("COUNSEL_REVIEW_REQUIRED", "Dokumen harus ditinjau konsultan hukum sebelum diterbitkan")
    if "id" not in texts:
        raise AgreementError("INDONESIAN_REQUIRED", "Versi Bahasa Indonesia wajib tersedia")
    seq = (session.execute(select(func.max(AgreementDocument.sequence)).where(AgreementDocument.kind == kind.value))
           .scalar() or 0) + 1
    docs = []
    for lang, text in texts.items():
        title, body = _title_and_body(text)
        doc = AgreementDocument(kind=kind.value, sequence=seq, version=version, language=lang, title=title, body=body,
                                content_hash=document_hash(body), material=material, counsel_reviewed=counsel_reviewed,
                                effective_at=effective_at, created_by=actor.user_id)
        session.add(doc)
        docs.append(doc)
    session.flush()
    audit.record(session, actor, "agreement.publish", "agreement", f"{kind.value}#{seq}",
                 {"version": version, "material": material, "counsel_reviewed": counsel_reviewed,
                  "hashes": {d.language: d.content_hash for d in docs}})
    return docs


def seed_default_agreements(session: Session, actor: Actor, *, production: bool) -> int:
    """Publish the packaged drafts (non-production only) if no documents exist yet."""
    if session.execute(select(AgreementDocument.id).limit(1)).first():
        return 0
    if production:
        raise AgreementError("COUNSEL_REVIEW_REQUIRED", "Draf bawaan tidak boleh diterbitkan di produksi")
    pkg = resources.files("idpatent.agreements")
    count = 0
    for kind, stem in _DEFAULT_FILES.items():
        texts = {lang: pkg.joinpath(f"{stem}.{lang}.md").read_text(encoding="utf-8") for lang in ("id", "en")}
        publish_version(session, kind, "0.1-draft", texts, material=True, effective_at=utcnow(),
                        counsel_reviewed=False, actor=actor, production=production)
        count += 1
    return count


def _versions(session: Session, now: datetime) -> list[VersionInfo]:
    rows = session.execute(
        select(AgreementDocument.kind, AgreementDocument.sequence, AgreementDocument.version,
               AgreementDocument.material, AgreementDocument.effective_at)
        .where(AgreementDocument.language == "id", AgreementDocument.effective_at <= now)
    ).all()
    return [VersionInfo(AgreementKind(r.kind), r.sequence, r.version, r.material, r.effective_at) for r in rows]


def _acceptances(session: Session, user_id: int) -> list[AcceptanceInfo]:
    rows = session.execute(
        select(AgreementAcceptance.kind, AgreementAcceptance.sequence).where(AgreementAcceptance.user_id == user_id)
    ).all()
    return [AcceptanceInfo(AgreementKind(r.kind), r.sequence) for r in rows]


def document_for(session: Session, kind: AgreementKind, sequence: int, lang: str) -> AgreementDocument | None:
    doc = session.execute(select(AgreementDocument).where(
        AgreementDocument.kind == kind.value, AgreementDocument.sequence == sequence, AgreementDocument.language == lang)).scalar()
    if doc is None and lang != "id":
        return document_for(session, kind, sequence, "id")
    return doc


def pending_documents(session: Session, user: User, firm: Firm | None, lang: str, now: datetime | None = None) -> list[AgreementDocument]:
    now = now or utcnow()
    required = required_kinds(Role(user.role), bool(firm and firm.client_agreement_required))
    pending = pending_kinds(required, _versions(session, now), _acceptances(session, user.id), now)
    return [d for v in pending if (d := document_for(session, v.kind, v.sequence, lang))]


def current_document(session: Session, kind: AgreementKind, lang: str, now: datetime | None = None) -> AgreementDocument | None:
    now = now or utcnow()
    seq = session.execute(select(func.max(AgreementDocument.sequence)).where(
        AgreementDocument.kind == kind.value, AgreementDocument.effective_at <= now)).scalar()
    return document_for(session, kind, seq, lang) if seq else None


@dataclass
class AcceptanceItem:
    document_id: int
    accepted: bool
    content_hash: str


def _acceptance_payload(a: AgreementAcceptance) -> dict:
    return {
        "user_id": a.user_id, "document_id": a.document_id, "kind": a.kind, "sequence": a.sequence,
        "version": a.version, "language": a.language, "content_hash": a.content_hash,
        "checkbox_text": a.checkbox_text, "on_behalf_of_firm_id": a.on_behalf_of_firm_id,
        "accepted_at": a.accepted_at.isoformat(), "ip": a.ip, "user_agent": a.user_agent,
    }


def accept(session: Session, user: User, firm: Firm | None, items: list[AcceptanceItem], *, ip: str | None,
           user_agent: str | None, now: datetime | None = None, optional_kinds: tuple[AgreementKind, ...] = ()) -> list[AgreementAcceptance]:
    """Record affirmative acceptance. Every pending document must be explicitly accepted with the hash shown."""
    now = now or utcnow()
    pending = pending_documents(session, user, firm, user.language, now)
    recorded_at = now.replace(microsecond=0)
    by_id = {i.document_id: i for i in items}
    missing = [d.kind for d in pending if not (by_id.get(d.id) and by_id[d.id].accepted is True)]
    if missing:
        raise AgreementError("ACCEPTANCE_REQUIRED", "Semua dokumen wajib harus disetujui secara eksplisit", missing)
    created = []
    for item in items:
        if item.accepted is not True:
            continue
        doc = session.get(AgreementDocument, item.document_id)
        if doc is None:
            raise AgreementError("DOCUMENT_NOT_FOUND", "Dokumen tidak ditemukan")
        kind = AgreementKind(doc.kind)
        if doc not in pending and kind not in optional_kinds and kind != AgreementKind.MARKETING_CONSENT:
            continue
        if item.content_hash != doc.content_hash:
            raise AgreementError("STALE_DOCUMENT", "Versi dokumen yang ditampilkan sudah tidak berlaku; muat ulang", [doc.kind])
        prev = session.execute(select(AgreementAcceptance.entry_hash).order_by(AgreementAcceptance.id.desc()).limit(1)).scalar()
        lang_idx = 0 if doc.language == "id" else 1
        row = AgreementAcceptance(
            user_id=user.id, document_id=doc.id, kind=doc.kind, sequence=doc.sequence, version=doc.version,
            language=doc.language, content_hash=doc.content_hash, checkbox_text=CHECKBOX_TEXT[kind][lang_idx],
            on_behalf_of_firm_id=user.firm_id if Role(user.role) in (Role.FIRM_ADMIN, Role.CLIENT_USER) else None,
            accepted_at=recorded_at, accepted_at_wib=format_datetime_wib(recorded_at, "id"), ip=ip, user_agent=(user_agent or "")[:400],
            prev_hash=prev or GENESIS_HASH, entry_hash="",
        )
        row.entry_hash = chain_hash(row.prev_hash, _acceptance_payload(row))
        session.add(row)
        session.flush()
        created.append(row)
    audit.record(session, Actor(user.id, f"user:{user.id}", user.firm_id, ip), "agreement.accept", "user", user.id,
                 {"documents": [f"{a.kind}#{a.sequence}:{a.language}" for a in created]}, user.firm_id)
    return created


def acceptance_history(session: Session, user_id: int) -> list[dict]:
    rows = session.execute(select(AgreementAcceptance).where(AgreementAcceptance.user_id == user_id)
                           .order_by(AgreementAcceptance.id)).scalars()
    return [
        {"kind": a.kind, "version": a.version, "sequence": a.sequence, "language": a.language,
         "content_hash": a.content_hash, "checkbox_text": a.checkbox_text, "accepted_at_wib": a.accepted_at_wib,
         "ip": a.ip, "user_agent": a.user_agent, "on_behalf_of_firm_id": a.on_behalf_of_firm_id,
         "entry_hash": a.entry_hash}
        for a in rows
    ]


def verify_acceptance_chain(session: Session):
    rows = session.execute(select(AgreementAcceptance).order_by(AgreementAcceptance.id)).scalars()
    return verify_chain((r.id, _acceptance_payload(r), r.prev_hash, r.entry_hash) for r in rows)
