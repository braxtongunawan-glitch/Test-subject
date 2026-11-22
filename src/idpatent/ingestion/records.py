"""Source-neutral patent record produced by every ingestion path."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..domain.enums import DataSource, LegalStatus, PatentType

_STATUS_TEXT: list[tuple[str, LegalStatus]] = [
    ("dianggap ditarik kembali", LegalStatus.WITHDRAWN),
    ("ditarik kembali", LegalStatus.WITHDRAWN),
    ("withdrawn", LegalStatus.WITHDRAWN),
    ("ditolak", LegalStatus.REFUSED),
    ("refused", LegalStatus.REFUSED),
    ("rejected", LegalStatus.REFUSED),
    ("dihapus", LegalStatus.LAPSED),
    ("lapsed", LegalStatus.LAPSED),
    ("berakhir", LegalStatus.EXPIRED),
    ("kedaluwarsa", LegalStatus.EXPIRED),
    ("kadaluarsa", LegalStatus.EXPIRED),
    ("expired", LegalStatus.EXPIRED),
    ("diberi", LegalStatus.GRANTED),
    ("granted", LegalStatus.GRANTED),
    ("berlaku", LegalStatus.GRANTED),
    ("aktif", LegalStatus.GRANTED),
    ("active", LegalStatus.GRANTED),
    ("in force", LegalStatus.GRANTED),
    ("dalam proses", LegalStatus.PENDING),
    ("pemeriksaan", LegalStatus.PENDING),
    ("pengumuman", LegalStatus.PENDING),
    ("permohonan", LegalStatus.PENDING),
    ("pending", LegalStatus.PENDING),
]

_TYPE_TEXT = {
    "paten sederhana": PatentType.PATEN_SEDERHANA,
    "sederhana": PatentType.PATEN_SEDERHANA,
    "simple patent": PatentType.PATEN_SEDERHANA,
    "simple": PatentType.PATEN_SEDERHANA,
    "utility model": PatentType.PATEN_SEDERHANA,
    "paten_sederhana": PatentType.PATEN_SEDERHANA,
    "ps": PatentType.PATEN_SEDERHANA,
    "paten": PatentType.PATEN,
    "patent": PatentType.PATEN,
    "standard": PatentType.PATEN,
    "p": PatentType.PATEN,
}


def parse_status(text: object) -> LegalStatus | None:
    if text is None or not str(text).strip():
        return None
    value = str(text).strip()
    try:
        return LegalStatus(value.upper())
    except ValueError:
        pass
    lower = value.lower()
    for needle, status in _STATUS_TEXT:
        if needle in lower:
            return status
    return LegalStatus.UNKNOWN


def parse_patent_type(text: object) -> PatentType | None:
    if text is None or not str(text).strip():
        return None
    value = str(text).strip()
    try:
        return PatentType(value.upper())
    except ValueError:
        pass
    lower = value.lower()
    if lower in _TYPE_TEXT:
        return _TYPE_TEXT[lower]
    if "sederhana" in lower or "simple" in lower:
        return PatentType.PATEN_SEDERHANA
    if "paten" in lower or "patent" in lower:
        return PatentType.PATEN
    raise ValueError(f"Jenis paten tidak dikenali: {value!r}")


@dataclass
class PatentRecord:
    source: DataSource
    application_number: str | None = None
    registration_number: str | None = None
    patent_type: PatentType | None = None
    title: str | None = None
    filing_date: date | None = None
    grant_date: date | None = None
    recorded_status: LegalStatus | None = None
    owner_name: str | None = None
    owner_country: str | None = None
    paid_through_year: int | None = None
    pct_application_number: str | None = None
    pct_publication_number: str | None = None
    priority_claims: list[dict] = field(default_factory=list)
    agent_name: str | None = None
    source_reference: str | None = None
    verified_on: date | None = None
    row_number: int | None = None
