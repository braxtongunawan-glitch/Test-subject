from datetime import date, datetime, timedelta, timezone

import pytest

from idpatent.compliance import tokens
from idpatent.compliance.audit_chain import GENESIS_HASH, chain_hash, verify_chain
from idpatent.compliance.clickwrap import AcceptanceInfo, VersionInfo, document_hash, pending_kinds, required_kinds
from idpatent.compliance.gate import ContactCompliance, SendContext, evaluate_send, is_personal_email
from idpatent.domain.enums import AgreementKind, LawfulBasis, Purpose, Relationship, Role
from idpatent.security import passwords, totp
from idpatent.security.crypto import FieldCipher, StaticKeyProvider, mask_email
from idpatent.security.rbac import Permission, has_permission


def test_password_hash_and_policy():
    h = passwords.hash_password("Kuat#Sekali2026")
    assert passwords.verify_password("Kuat#Sekali2026", h)
    assert not passwords.verify_password("salah", h)
    assert "PASSWORD_TOO_SHORT" in passwords.complexity_errors("Ab1!")
    assert "PASSWORD_NEEDS_3_CHARACTER_CLASSES" in passwords.complexity_errors("alllowercaseletters")
    assert "PASSWORD_CONTAINS_EMAIL" in passwords.complexity_errors("Budisantoso#2026", email="budisantoso@firma.co.id")
    assert passwords.complexity_errors("Kuat#Sekali2026") == []
    assert passwords.reused("Kuat#Sekali2026", [h])


def test_totp_rfc6238_vector_and_replay():
    # RFC 6238 SHA-1 test secret "12345678901234567890", T=59 -> 94287082 (8 digits) -> 287082 (6 digits)
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.totp(secret, at=59) == "287082"
    step = totp.verify(secret, "287082", at=59)
    assert step is not None
    assert totp.verify(secret, "287082", at=59, last_used_step=step) is None
    assert totp.verify(secret, "000000", at=59) is None
    assert totp.provisioning_uri(secret, "a@b.co", "IDPatent").startswith("otpauth://totp/")


def test_field_encryption_and_blind_index():
    cipher = FieldCipher(StaticKeyProvider.ephemeral())
    token = cipher.encrypt("budi@contoh.co.id", "contact.email")
    assert "budi" not in token
    assert cipher.decrypt(token, "contact.email") == "budi@contoh.co.id"
    with pytest.raises(Exception):
        cipher.decrypt(token, "contact.phone")  # context bound as associated data
    assert cipher.blind_index("Budi@Contoh.co.id ") == cipher.blind_index("budi@contoh.co.id")
    assert mask_email("budi.santoso@contoh.co.id") == "b***o@contoh.co.id"


def test_rbac_separation_of_duties():
    assert has_permission(Role.ACCOUNT_MANAGER, Permission.COMM_SEND)
    assert not has_permission(Role.ACCOUNT_MANAGER, Permission.COMM_APPROVE)
    assert not has_permission(Role.VIEWER, Permission.CONTACT_READ_PII)
    assert has_permission(Role.FIRM_ADMIN, Permission.TEMPLATE_APPROVE)
    assert not has_permission(Role.FIRM_ADMIN, Permission.PLATFORM_ADMIN)
    assert has_permission(Role.CLIENT_USER, Permission.PORTAL_SELF)
    assert not has_permission(Role.CLIENT_USER, Permission.PATENT_READ)


def test_audit_chain_detects_tampering():
    payloads = [{"action": "login", "n": i} for i in range(3)]
    rows, prev = [], GENESIS_HASH
    for i, p in enumerate(payloads):
        h = chain_hash(prev, p)
        rows.append((i, p, prev, h))
        prev = h
    assert verify_chain(rows).ok
    tampered = list(rows)
    tampered[1] = (1, {"action": "login", "n": 99}, rows[1][2], rows[1][3])
    result = verify_chain(tampered)
    assert not result.ok and result.first_broken_id == 1
    assert not verify_chain([rows[0], rows[2]]).ok  # deletion detected


def test_clickwrap_material_changes_force_reacceptance():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    versions = [
        VersionInfo(AgreementKind.TERMS_OF_SERVICE, 1, "1.0", True, t0),
        VersionInfo(AgreementKind.TERMS_OF_SERVICE, 2, "1.1", False, t0 + timedelta(days=30)),
        VersionInfo(AgreementKind.PRIVACY_POLICY, 1, "1.0", True, t0),
        VersionInfo(AgreementKind.PRIVACY_POLICY, 2, "2.0", True, t0 + timedelta(days=60)),
    ]
    accepted = [AcceptanceInfo(AgreementKind.TERMS_OF_SERVICE, 1), AcceptanceInfo(AgreementKind.PRIVACY_POLICY, 1)]
    now = t0 + timedelta(days=90)
    required = required_kinds(Role.CLIENT_USER)
    pending = pending_kinds(required, versions, accepted, now)
    assert [(p.kind, p.version) for p in pending] == [(AgreementKind.PRIVACY_POLICY, "2.0")]
    # before the material privacy update takes effect nothing is pending
    assert pending_kinds(required, versions, accepted, t0 + timedelta(days=45)) == []
    assert AgreementKind.DATA_PROCESSING_NOTICE in required_kinds(Role.ACCOUNT_MANAGER)
    assert document_hash("a \r\nb  \n") == document_hash("a\nb")


def test_tokens_sign_verify():
    key = b"k" * 32
    t = tokens.sign({"c": 5, "p": "ALL"}, key)
    assert tokens.verify(t, key)["c"] == 5
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(t[:-2] + "xx", key)
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(tokens.sign({"c": 1}, key, ttl_seconds=-1), key)


def ctx(**kw):
    base = dict(purpose=Purpose.ANNUITY_REMINDER, patent_count=1, recipient_count=1, campaign_approved=False,
                template_approved=True, sender_identity_complete=True, production=True, today=date(2026, 11, 22))
    base.update(kw)
    return SendContext(**base)


def contact(**kw):
    base = dict(relationship=Relationship.CLIENT, email_domain_personal=False, lawful_basis=LawfulBasis.CONTRACT)
    base.update(kw)
    return ContactCompliance(**base)


def test_gate_client_contract_reminder_allowed():
    d = evaluate_send(contact(), ctx())
    assert d.allowed and d.lawful_basis == LawfulBasis.CONTRACT and not d.is_cold


def test_gate_blocks_suppressed_and_opted_out():
    assert "SUPPRESSED" in evaluate_send(contact(suppressed=True), ctx()).blocked_reasons
    d = evaluate_send(contact(opted_out_purposes=frozenset({Purpose.ANNUITY_REMINDER})), ctx())
    assert "OPTED_OUT" in d.blocked_reasons
    d = evaluate_send(contact(consent={Purpose.ANNUITY_REMINDER: False}), ctx())
    assert "CONSENT_WITHDRAWN" in d.blocked_reasons


def test_gate_commercial_offer_to_client_needs_consent():
    d = evaluate_send(contact(), ctx(purpose=Purpose.COMMERCIAL_OFFER))
    assert "COMMERCIAL_OFFER_NEEDS_CONSENT_OR_LIA" in d.blocked_reasons
    d = evaluate_send(contact(consent={Purpose.COMMERCIAL_OFFER: True}), ctx(purpose=Purpose.COMMERCIAL_OFFER))
    assert d.allowed and d.lawful_basis == LawfulBasis.CONSENT


def test_gate_cold_outreach_rules():
    cold = dict(relationship=Relationship.PROSPECT, lawful_basis=LawfulBasis.LEGITIMATE_INTEREST, lia_approved=True,
                lia_review_due=date(2027, 6, 1))
    d = evaluate_send(contact(**cold), ctx())
    assert d.is_cold and d.requires_approval and "AWAITING_APPROVAL" in d.blocked_reasons
    d = evaluate_send(contact(**cold), ctx(campaign_approved=True))
    assert d.allowed and "COLD_OUTREACH_GREY_AREA_UU_PDP" in d.warnings
    d = evaluate_send(contact(**cold, email_domain_personal=True), ctx(campaign_approved=True))
    assert "COLD_OUTREACH_TO_PERSONAL_EMAIL" in d.blocked_reasons
    d = evaluate_send(contact(**cold), ctx(campaign_approved=True, purpose=Purpose.COMMERCIAL_OFFER))
    assert "COLD_PURE_COMMERCIAL_OFFER" in d.blocked_reasons
    d = evaluate_send(contact(**cold), ctx(campaign_approved=True, cold_sent_today=50))
    assert "COLD_DAILY_CAP_REACHED" in d.blocked_reasons
    d = evaluate_send(contact(**{**cold, "lia_review_due": date(2026, 1, 1)}), ctx(campaign_approved=True))
    assert "LIA_REVIEW_OVERDUE" in d.blocked_reasons
    d = evaluate_send(contact(**cold), ctx(campaign_approved=True, patent_count=0))
    assert "COLD_OUTREACH_MUST_REFERENCE_PATENT" in d.blocked_reasons


def test_gate_misc_blocks():
    assert "NO_LAWFUL_BASIS" in evaluate_send(contact(lawful_basis=None), ctx()).blocked_reasons
    assert "AWAITING_APPROVAL" in evaluate_send(contact(), ctx(recipient_count=25)).blocked_reasons
    assert "TEMPLATE_NOT_COUNSEL_APPROVED" in evaluate_send(contact(), ctx(template_approved=False)).blocked_reasons
    dev = evaluate_send(contact(), ctx(template_approved=False, production=False))
    assert dev.allowed and "TEMPLATE_NOT_COUNSEL_APPROVED" in dev.warnings
    assert "SENDER_IDENTITY_INCOMPLETE" in evaluate_send(contact(), ctx(sender_identity_complete=False)).blocked_reasons
    assert is_personal_email("x@Yahoo.co.id") and not is_personal_email("x@itb.ac.id")
