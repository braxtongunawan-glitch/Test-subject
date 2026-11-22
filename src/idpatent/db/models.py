"""Relational schema (SRS section 7) with provenance and audit fields.

Conventions:
* All timestamps are stored as UTC; display converts to WIB.
* Personal data of contacts (name, e-mail, phone) is encrypted at field level;
  ``email_index`` is a keyed blind index for lookups and suppression checks.
* ``audit_log``, ``agreement_acceptance`` and ``consent_event`` are append-only
  (enforced by triggers created in ``idpatent.db.session``) and hash-chained.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """Stores naive UTC, always returns timezone-aware UTC (SQLite drops tzinfo)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Firm(Base):
    __tablename__ = "firm"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    legal_name: Mapped[str] = mapped_column(String(300))
    address: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    website: Mapped[str | None] = mapped_column(String(300))
    alert_policy: Mapped[dict | None] = mapped_column(JSON)
    services_id: Mapped[list] = mapped_column(JSON, default=list)  # service offer lines, Bahasa Indonesia
    services_en: Mapped[list] = mapped_column(JSON, default=list)
    cold_daily_cap: Mapped[int] = mapped_column(Integer, default=50)
    bulk_threshold: Mapped[int] = mapped_column(Integer, default=10)
    client_agreement_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int | None] = mapped_column(ForeignKey("firm.id"))
    email_index: Mapped[str] = mapped_column(String(64), unique=True)
    email_enc: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(30))
    language: Mapped[str] = mapped_column(String(2), default="id")
    password_hash: Mapped[str] = mapped_column(String(200))
    password_history: Mapped[list] = mapped_column(JSON, default=list)
    password_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_last_step: Mapped[int | None] = mapped_column(Integer)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    owner_scope: Mapped[list] = mapped_column(JSON, default=list)  # CLIENT_USER: owner ids visible
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuthSession(Base):
    __tablename__ = "auth_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(20), default="SESSION")  # SESSION | MFA_CHALLENGE
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    user: Mapped[User] = relationship()


class Owner(Base):
    __tablename__ = "owner"
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firm.id"), index=True)
    name: Mapped[str] = mapped_column(String(400))
    normalized_key: Mapped[str] = mapped_column(String(400), index=True)
    owner_type: Mapped[str] = mapped_column(String(30))
    country: Mapped[str | None] = mapped_column(String(2))
    legal_forms: Mapped[list] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    match_suggestions: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Patent(Base):
    __tablename__ = "patent"
    __table_args__ = (
        UniqueConstraint("firm_id", "application_number", name="uq_patent_firm_application"),
        UniqueConstraint("firm_id", "registration_number", name="uq_patent_firm_registration"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firm.id"), index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("owner.id"), index=True)
    application_number: Mapped[str | None] = mapped_column(String(20), index=True)
    registration_number: Mapped[str | None] = mapped_column(String(20), index=True)
    patent_type: Mapped[str] = mapped_column(String(20))
    is_pct_national_phase: Mapped[bool] = mapped_column(Boolean, default=False)
    pct_application_number: Mapped[str | None] = mapped_column(String(30))
    pct_publication_number: Mapped[str | None] = mapped_column(String(30))
    priority_claims: Mapped[list] = mapped_column(JSON, default=list)
    family_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    filing_date: Mapped[date] = mapped_column(Date)
    grant_date: Mapped[date | None] = mapped_column(Date)
    recorded_status: Mapped[str | None] = mapped_column(String(30))
    paid_through_year: Mapped[int | None] = mapped_column(Integer)
    agent_name: Mapped[str | None] = mapped_column(String(300))  # konsultan HKI
    # provenance
    source: Mapped[str] = mapped_column(String(30))
    source_reference: Mapped[str | None] = mapped_column(Text)
    last_verified_on: Mapped[date | None] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(String(20), default="UNVERIFIED")
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    # computed cache (refreshed on every change and by the daily alert run)
    protection_end: Mapped[date | None] = mapped_column(Date, index=True)
    computed_status: Mapped[str | None] = mapped_column(String(30), index=True)
    next_annuity_due: Mapped[date | None] = mapped_column(Date, index=True)
    next_grace_end: Mapped[date | None] = mapped_column(Date)
    next_ws_due: Mapped[date | None] = mapped_column(Date, index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    confidence_band: Mapped[str | None] = mapped_column(String(10))
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    rules_version: Mapped[str | None] = mapped_column(String(32))
    computed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    owner: Mapped[Owner | None] = relationship()
    payments: Mapped[list["AnnuityPayment"]] = relationship(back_populates="patent", cascade="all, delete-orphan")
    ws_filings: Mapped[list["WorkingStatementFiling"]] = relationship(back_populates="patent", cascade="all, delete-orphan")

    @property
    def display_number(self) -> str:
        return self.registration_number or self.application_number or f"#{self.id}"


class AnnuityPayment(Base):
    __tablename__ = "annuity_payment"
    id: Mapped[int] = mapped_column(primary_key=True)
    patent_id: Mapped[int] = mapped_column(ForeignKey("patent.id"), index=True)
    first_year: Mapped[int] = mapped_column(Integer)
    last_year: Mapped[int] = mapped_column(Integer)
    paid_on: Mapped[date] = mapped_column(Date)
    amount_idr: Mapped[int | None] = mapped_column(Integer)
    surcharge_idr: Mapped[int | None] = mapped_column(Integer)
    reference: Mapped[str | None] = mapped_column(String(200))  # SIMPAKI billing code / receipt
    source: Mapped[str] = mapped_column(String(30))
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    patent: Mapped[Patent] = relationship(back_populates="payments")


class WorkingStatementFiling(Base):
    __tablename__ = "working_statement_filing"
    __table_args__ = (UniqueConstraint("patent_id", "period_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    patent_id: Mapped[int] = mapped_column(ForeignKey("patent.id"), index=True)
    period_key: Mapped[str] = mapped_column(String(10))
    submitted_on: Mapped[date] = mapped_column(Date)
    declaration: Mapped[str] = mapped_column(String(20))  # MANUFACTURED | IMPORTED | LICENSED | NOT_WORKED
    reason: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(200))
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    patent: Mapped[Patent] = relationship(back_populates="ws_filings")


class Alert(Base):
    __tablename__ = "alert"
    __table_args__ = (UniqueConstraint("patent_id", "dedupe_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firm.id"), index=True)
    patent_id: Mapped[int] = mapped_column(ForeignKey("patent.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    reference: Mapped[str] = mapped_column(String(100))
    stage: Mapped[str] = mapped_column(String(20))
    dedupe_key: Mapped[str] = mapped_column(String(200))
    target_date: Mapped[date] = mapped_column(Date)
    trigger_date: Mapped[date] = mapped_column(Date)
    severity: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(15), default="OPEN", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    acknowledged_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    patent: Mapped[Patent] = relationship()


class LegitimateInterestAssessment(Base):
    __tablename__ = "lia"
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firm.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    purpose: Mapped[str] = mapped_column(Text)
    necessity: Mapped[str] = mapped_column(Text)
    balancing: Mapped[str] = mapped_column(Text)
    safeguards: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    review_due: Mapped[date | None] = mapped_column(Date)


class Contact(Base):
    __tablename__ = "contact"
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firm.id"), index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("owner.id"), index=True)
    name_enc: Mapped[str | None] = mapped_column(Text)
    email_enc: Mapped[str] = mapped_column(Text)
    email_index: Mapped[str] = mapped_column(String(64), index=True)
    email_domain: Mapped[str] = mapped_column(String(255))
    phone_enc: Mapped[str | None] = mapped_column(Text)
    role_title: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(2), default="id")
    relationship_type: Mapped[str] = mapped_column("relationship", String(20))
    lawful_basis: Mapped[str | None] = mapped_column(String(30))
    lia_id: Mapped[int | None] = mapped_column(ForeignKey("lia.id"))
    basis_note: Mapped[str | None] = mapped_column(Text)  # documentation of how the contact was obtained
    is_personal_email: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_all: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_purposes: Mapped[list] = mapped_column(JSON, default=list)
    erased_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    owner: Mapped[Owner | None] = relationship()
    lia: Mapped[LegitimateInterestAssessment | None] = relationship()


class ConsentEvent(Base):
    """Append-only consent ledger; the latest event per (contact, purpose) wins."""

    __tablename__ = "consent_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(40))
    granted: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(30))  # PREFERENCE_CENTER | OPT_OUT_LINK | STAFF | CLICKWRAP | IMPORT
    evidence: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(64))
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SuppressionEntry(Base):
    __tablename__ = "suppression"
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int | None] = mapped_column(ForeignKey("firm.id"), index=True)  # NULL = global
    email_index: Mapped[str | None] = mapped_column(String(64), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    reason: Mapped[str] = mapped_column(String(30))  # OPT_OUT | HARD_BOUNCE | COMPLAINT | MANUAL | ERASURE
    created_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TemplateApproval(Base):
    """Attestation that Indonesian counsel reviewed a template at a specific content hash."""

    __tablename__ = "template_approval"
    __table_args__ = (UniqueConstraint("template_key", "language", "content_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    template_key: Mapped[str] = mapped_column(String(60))
    language: Mapped[str] = mapped_column(String(2))
    content_hash: Mapped[str] = mapped_column(String(64))
    counsel_name: Mapped[str] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Campaign(Base):
    __tablename__ = "campaign"
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firm.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    purpose: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    is_cold: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    review_note: Mapped[str | None] = mapped_column(Text)
    communications: Mapped[list["Communication"]] = relationship(back_populates="campaign")


class Communication(Base):
    __tablename__ = "communication"
    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firm.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaign.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    patent_ids: Mapped[list] = mapped_column(JSON, default=list)
    purpose: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(2))
    template_key: Mapped[str] = mapped_column(String(60))
    template_hash: Mapped[str | None] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(String(300))
    body_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    blocked_reasons: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    lawful_basis: Mapped[str | None] = mapped_column(String(30))
    is_cold: Mapped[bool] = mapped_column(Boolean, default=False)
    message_id: Mapped[str | None] = mapped_column(String(300), index=True)
    provider: Mapped[str | None] = mapped_column(String(30))
    provider_message_id: Mapped[str | None] = mapped_column(String(300), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(300), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    campaign: Mapped[Campaign | None] = relationship(back_populates="communications")
    contact: Mapped[Contact] = relationship()


class CommunicationEvent(Base):
    __tablename__ = "communication_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    communication_id: Mapped[int] = mapped_column(ForeignKey("communication.id"), index=True)
    event: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class AgreementDocument(Base):
    __tablename__ = "agreement_document"
    __table_args__ = (UniqueConstraint("kind", "sequence", "language"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    sequence: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(2))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    material: Mapped[bool] = mapped_column(Boolean, default=True)
    counsel_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AgreementAcceptance(Base):
    __tablename__ = "agreement_acceptance"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("agreement_document.id"))
    kind: Mapped[str] = mapped_column(String(40))
    sequence: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(2))
    content_hash: Mapped[str] = mapped_column(String(64))
    checkbox_text: Mapped[str] = mapped_column(Text)
    on_behalf_of_firm_id: Mapped[int | None] = mapped_column(ForeignKey("firm.id"))
    accepted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    accepted_at_wib: Mapped[str] = mapped_column(String(40))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    firm_id: Mapped[int | None] = mapped_column(Integer, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    actor_label: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))
