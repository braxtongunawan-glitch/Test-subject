"""Click-wrap agreement logic (SRS section 11).

* Documents are versioned per kind; each language version has its own content
  hash, and acceptance records the exact hash the user was shown.
* ``material`` versions force re-acceptance; non-material edits (typos,
  formatting) do not, provided the user accepted the latest material version
  or something newer.
* Acceptance must be an explicit affirmative action per document: the
  submission carries ``accepted=True`` for each pending document, and the
  server never defaults it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from ..domain.enums import AgreementKind, Role

FIRM_ROLES = frozenset({Role.SUPER_ADMIN, Role.FIRM_ADMIN, Role.ACCOUNT_MANAGER, Role.VIEWER})
DATA_HANDLING_ROLES = frozenset({Role.SUPER_ADMIN, Role.FIRM_ADMIN, Role.ACCOUNT_MANAGER})

CHECKBOX_TEXT = {
    AgreementKind.TERMS_OF_SERVICE: (
        "Saya telah membaca dan menyetujui Syarat dan Ketentuan Penggunaan.",
        "I have read and agree to the Terms of Service.",
    ),
    AgreementKind.PRIVACY_POLICY: (
        "Saya telah membaca dan memahami Kebijakan Privasi.",
        "I have read and understood the Privacy Policy.",
    ),
    AgreementKind.DATA_PROCESSING_NOTICE: (
        "Saya menyatakan berwenang mengunggah dan mengelola data pemilik paten pihak ketiga sesuai pemberitahuan ini.",
        "I confirm I am authorised to upload and manage third-party patent owner data as described in this notice.",
    ),
    AgreementKind.MARKETING_CONSENT: (
        "Saya setuju menerima informasi dan penawaran layanan terkait paten (opsional, dapat ditarik kapan saja).",
        "I agree to receive patent-related information and service offers (optional, may be withdrawn at any time).",
    ),
    AgreementKind.CLIENT_SERVICE_AGREEMENT: (
        "Saya menyetujui Perjanjian Layanan Klien atas nama organisasi saya.",
        "I accept the Client Service Agreement on behalf of my organisation.",
    ),
}


def document_hash(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def required_kinds(role: Role, client_agreement_required: bool = False) -> set[AgreementKind]:
    kinds = {AgreementKind.TERMS_OF_SERVICE, AgreementKind.PRIVACY_POLICY}
    if role in DATA_HANDLING_ROLES:
        kinds.add(AgreementKind.DATA_PROCESSING_NOTICE)
    if client_agreement_required and role in (Role.FIRM_ADMIN, Role.CLIENT_USER):
        kinds.add(AgreementKind.CLIENT_SERVICE_AGREEMENT)
    return kinds


@dataclass(frozen=True)
class VersionInfo:
    kind: AgreementKind
    sequence: int  # monotonically increasing per kind
    version: str
    material: bool
    effective_at: datetime


@dataclass(frozen=True)
class AcceptanceInfo:
    kind: AgreementKind
    sequence: int


def pending_kinds(
    required: set[AgreementKind],
    versions: list[VersionInfo],
    acceptances: list[AcceptanceInfo],
    now: datetime,
) -> list[VersionInfo]:
    """Return the current version of each required kind the user still has to accept."""
    pending = []
    for kind in sorted(required, key=lambda k: k.value):
        effective = sorted(
            (v for v in versions if v.kind == kind and v.effective_at <= now), key=lambda v: v.sequence
        )
        if not effective:
            continue
        current = effective[-1]
        last_material = max((v.sequence for v in effective if v.material), default=effective[0].sequence)
        accepted = max((a.sequence for a in acceptances if a.kind == kind), default=None)
        if accepted is None or accepted < last_material:
            pending.append(current)
    return pending
