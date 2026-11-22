"""Pre-send compliance gate (SRS sections 5 and 10.5).

Every outbound message passes :func:`evaluate_send` immediately before it is
handed to the e-mail provider. The gate is deliberately conservative: when in
doubt it blocks and records why, so a person can review.

Rules enforced:

1. Suppression list (global and firm-level) always wins.
2. Purpose-level opt-out and withdrawn consent always win.
3. A lawful basis under UU PDP must be established for the purpose:
   * CONSENT – an active consent record for this purpose;
   * CONTRACT – an existing client relationship (not for commercial offers);
   * LEGITIMATE_INTEREST – requires an approved, unexpired Legitimate Interest
     Assessment (LIA); for prospects it is limited to patent-specific messages
     to institutional (non-personal) addresses;
   * LEGAL_OBLIGATION – only for notices the firm is legally required to send.
4. Patent-specific purposes must reference at least one concrete patent.
5. Cold (prospect) or bulk sends require campaign approval in the review queue.
6. Cold sends are capped per firm per day.
7. Sender identity (legal name and physical address) must be complete.
8. In production, templates must carry a counsel approval matching their
   current content hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..domain.enums import PATENT_SPECIFIC_PURPOSES, LawfulBasis, Purpose, Relationship

PERSONAL_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.id", "ymail.com", "rocketmail.com",
        "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
        "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com", "qq.com", "163.com", "naver.com",
    }
)


def is_personal_email(email: str) -> bool:
    return email.rsplit("@", 1)[-1].strip().lower() in PERSONAL_EMAIL_DOMAINS


@dataclass(frozen=True)
class ContactCompliance:
    relationship: Relationship
    email_domain_personal: bool
    suppressed: bool = False
    opted_out_purposes: frozenset[Purpose] = frozenset()
    opted_out_all: bool = False
    consent: dict[Purpose, bool] = field(default_factory=dict)  # latest consent decision per purpose
    lawful_basis: LawfulBasis | None = None
    lia_approved: bool = False
    lia_review_due: date | None = None


@dataclass(frozen=True)
class SendContext:
    purpose: Purpose
    patent_count: int
    recipient_count: int
    campaign_approved: bool
    template_approved: bool
    sender_identity_complete: bool
    production: bool
    cold_sent_today: int = 0
    cold_daily_cap: int = 50
    bulk_threshold: int = 10
    today: date | None = None


@dataclass
class SendDecision:
    allowed: bool
    lawful_basis: LawfulBasis | None
    is_cold: bool
    requires_approval: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def requires_approval(relationship: Relationship, recipient_count: int, bulk_threshold: int) -> bool:
    return relationship == Relationship.PROSPECT or recipient_count >= bulk_threshold


def evaluate_send(contact: ContactCompliance, ctx: SendContext) -> SendDecision:
    blocked: list[str] = []
    warnings: list[str] = []
    is_cold = contact.relationship == Relationship.PROSPECT
    today = ctx.today or date.today()

    if contact.suppressed:
        blocked.append("SUPPRESSED")
    if contact.opted_out_all or ctx.purpose in contact.opted_out_purposes:
        blocked.append("OPTED_OUT")
    if contact.consent.get(ctx.purpose) is False:
        blocked.append("CONSENT_WITHDRAWN")

    basis = _resolve_basis(contact, ctx, is_cold, today, blocked, warnings)

    if ctx.purpose in PATENT_SPECIFIC_PURPOSES and ctx.patent_count < 1:
        blocked.append("NO_PATENT_REFERENCE")
    if is_cold and ctx.patent_count < 1:
        blocked.append("COLD_OUTREACH_MUST_REFERENCE_PATENT")

    approval = requires_approval(contact.relationship, ctx.recipient_count, ctx.bulk_threshold)
    if approval and not ctx.campaign_approved:
        blocked.append("AWAITING_APPROVAL")
    if is_cold and ctx.cold_sent_today >= ctx.cold_daily_cap:
        blocked.append("COLD_DAILY_CAP_REACHED")
    if not ctx.sender_identity_complete:
        blocked.append("SENDER_IDENTITY_INCOMPLETE")
    if not ctx.template_approved:
        if ctx.production:
            blocked.append("TEMPLATE_NOT_COUNSEL_APPROVED")
        else:
            warnings.append("TEMPLATE_NOT_COUNSEL_APPROVED")

    return SendDecision(
        allowed=not blocked,
        lawful_basis=basis if not blocked else None,
        is_cold=is_cold,
        requires_approval=approval,
        blocked_reasons=list(dict.fromkeys(blocked)),
        warnings=warnings,
    )


def _resolve_basis(
    contact: ContactCompliance,
    ctx: SendContext,
    is_cold: bool,
    today: date,
    blocked: list[str],
    warnings: list[str],
) -> LawfulBasis | None:
    if contact.consent.get(ctx.purpose) is True:
        return LawfulBasis.CONSENT

    basis = contact.lawful_basis
    if basis == LawfulBasis.CONSENT:
        blocked.append("NO_ACTIVE_CONSENT_FOR_PURPOSE")
        return None
    if basis == LawfulBasis.CONTRACT:
        if contact.relationship not in (Relationship.CLIENT, Relationship.AGENT):
            blocked.append("CONTRACT_BASIS_WITHOUT_RELATIONSHIP")
            return None
        if ctx.purpose == Purpose.COMMERCIAL_OFFER:
            blocked.append("COMMERCIAL_OFFER_NEEDS_CONSENT_OR_LIA")
            return None
        return LawfulBasis.CONTRACT
    if basis == LawfulBasis.LEGAL_OBLIGATION:
        if ctx.purpose == Purpose.COMMERCIAL_OFFER:
            blocked.append("COMMERCIAL_OFFER_NOT_LEGAL_OBLIGATION")
            return None
        return LawfulBasis.LEGAL_OBLIGATION
    if basis == LawfulBasis.LEGITIMATE_INTEREST:
        if not contact.lia_approved:
            blocked.append("LIA_NOT_APPROVED")
            return None
        if contact.lia_review_due and contact.lia_review_due < today:
            blocked.append("LIA_REVIEW_OVERDUE")
            return None
        if is_cold:
            if contact.email_domain_personal:
                blocked.append("COLD_OUTREACH_TO_PERSONAL_EMAIL")
                return None
            if ctx.purpose == Purpose.COMMERCIAL_OFFER:
                blocked.append("COLD_PURE_COMMERCIAL_OFFER")
                return None
            warnings.append("COLD_OUTREACH_GREY_AREA_UU_PDP")
        return LawfulBasis.LEGITIMATE_INTEREST
    blocked.append("NO_LAWFUL_BASIS")
    return None
