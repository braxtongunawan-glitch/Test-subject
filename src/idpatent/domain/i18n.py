"""Bahasa Indonesia (primary) and English display text.

All user-facing labels pass through :func:`label` / :func:`warning_text` /
:func:`alert_title`, so the UI, reports and emails share one vocabulary of
local legal terminology.
"""

from __future__ import annotations

from enum import Enum

from .enums import (
    AlertKind,
    ConfidenceBand,
    DataSource,
    InstallmentStatus,
    LawfulBasis,
    LegalStatus,
    ObligationStatus,
    OwnerType,
    PatentType,
    Purpose,
    Severity,
)

_LABELS: dict[Enum, tuple[str, str]] = {
    PatentType.PATEN: ("Paten", "Patent"),
    PatentType.PATEN_SEDERHANA: ("Paten Sederhana", "Simple Patent"),
    LegalStatus.PENDING: ("Dalam Proses", "Pending"),
    LegalStatus.GRANTED: ("Diberi", "Granted / Active"),
    LegalStatus.EXPIRED: ("Berakhir", "Expired"),
    LegalStatus.LAPSED: ("Dihapus", "Lapsed / Removed"),
    LegalStatus.LAPSE_SUSPECTED: ("Berisiko Dihapus", "Lapse Suspected"),
    LegalStatus.REFUSED: ("Ditolak", "Refused"),
    LegalStatus.WITHDRAWN: ("Ditarik Kembali", "Withdrawn"),
    LegalStatus.UNKNOWN: ("Tidak Diketahui", "Unknown"),
    InstallmentStatus.PAID: ("Lunas", "Paid"),
    InstallmentStatus.UPCOMING: ("Akan Jatuh Tempo", "Upcoming"),
    InstallmentStatus.IN_GRACE: ("Masa Tenggang", "In Grace Period"),
    InstallmentStatus.MISSED: ("Terlewat", "Missed"),
    InstallmentStatus.UNKNOWN: ("Status Pembayaran Belum Diverifikasi", "Payment Not Verified"),
    ObligationStatus.SUBMITTED: ("Sudah Disampaikan", "Submitted"),
    ObligationStatus.UPCOMING: ("Akan Jatuh Tempo", "Upcoming"),
    ObligationStatus.OVERDUE: ("Terlambat", "Overdue"),
    DataSource.PDKI: ("Ekstrak PDKI", "PDKI extract"),
    DataSource.SAKI: ("Sistem SAKI DJKI", "DGIP SAKI system"),
    DataSource.MANUAL_VERIFIED: ("Verifikasi Manual", "Manual verification"),
    DataSource.AGENT_FEED: ("Data Konsultan HKI", "IP agent feed"),
    DataSource.USER_INPUT: ("Input Pengguna", "User input"),
    DataSource.SUPPLEMENTARY: ("Sumber Tambahan", "Supplementary source"),
    OwnerType.COMPANY: ("Perusahaan", "Company"),
    OwnerType.UNIVERSITY: ("Perguruan Tinggi", "University"),
    OwnerType.RESEARCH_GOVERNMENT: ("Lembaga Litbang / Pemerintah", "Research / Government"),
    OwnerType.INDIVIDUAL: ("Perorangan", "Individual"),
    OwnerType.FOREIGN: ("Pemohon Asing", "Foreign"),
    OwnerType.UNKNOWN: ("Tidak Diketahui", "Unknown"),
    ConfidenceBand.HIGH: ("Tinggi", "High"),
    ConfidenceBand.MEDIUM: ("Sedang", "Medium"),
    ConfidenceBand.LOW: ("Rendah", "Low"),
    Severity.INFO: ("Info", "Info"),
    Severity.WARNING: ("Peringatan", "Warning"),
    Severity.CRITICAL: ("Kritis", "Critical"),
    Purpose.ANNUITY_REMINDER: ("Pengingat biaya tahunan paten", "Patent annuity reminder"),
    Purpose.WORKING_STATEMENT_REMINDER: ("Pengingat pernyataan pelaksanaan paten", "Patent working statement reminder"),
    Purpose.EXPIRY_NOTICE: ("Pemberitahuan berakhirnya pelindungan paten", "Patent expiry notice"),
    Purpose.STATUS_NOTICE: ("Pemberitahuan perubahan status paten", "Patent status notice"),
    Purpose.COMMERCIAL_OFFER: ("Penawaran layanan", "Service offer"),
    LawfulBasis.CONSENT: ("Persetujuan", "Consent"),
    LawfulBasis.CONTRACT: ("Pelaksanaan perjanjian", "Performance of a contract"),
    LawfulBasis.LEGAL_OBLIGATION: ("Kewajiban hukum", "Legal obligation"),
    LawfulBasis.LEGITIMATE_INTEREST: ("Kepentingan yang sah", "Legitimate interest"),
}

_WARNINGS: dict[str, tuple[str, str]] = {
    "RULES_FALLBACK_PRE_2001": (
        "Tanggal penerimaan sebelum UU 14/2001; aturan lama tidak dimodelkan. Verifikasi manual diperlukan.",
        "Filing predates Law 14/2001; earlier rules are not modelled. Manual verification required.",
    ),
    "ANNUITY_RULES_APPROXIMATED": (
        "Jadwal biaya tahunan sebelum UU 13/2016 diperkirakan dengan aturan yang lebih baru.",
        "Annuity timing before Law 13/2016 is approximated with later rules.",
    ),
    "RULES_NOT_COUNSEL_VERIFIED": (
        "Parameter hukum belum diverifikasi oleh konsultan hukum Indonesia.",
        "Legal rule parameters have not yet been verified by Indonesian counsel.",
    ),
    "PAYMENT_STATUS_UNKNOWN": (
        "Status pembayaran biaya tahunan sebelumnya belum diketahui.",
        "Payment status of past annuities is unknown.",
    ),
    "GRANT_DATE_MISSING": (
        "Status tercatat Diberi tetapi tanggal pemberian tidak tersedia; jadwal biaya tahunan tidak dapat dihitung.",
        "Recorded as granted but the grant date is missing; annuities cannot be scheduled.",
    ),
    "GRANT_AFTER_TERM_END": ("Tanggal pemberian setelah akhir masa pelindungan.", "Grant date falls after term end."),
    "RECORDED_EXPIRED_BEFORE_TERM_END": (
        "Status tercatat Berakhir sebelum akhir masa pelindungan terhitung.",
        "Recorded as expired before the computed term end.",
    ),
    "RECORDED_PENDING_BUT_GRANT_DATE_SET": (
        "Status tercatat Dalam Proses tetapi tanggal pemberian tersedia.",
        "Recorded as pending but a grant date is present.",
    ),
    "PAID_THROUGH_BEYOND_TERM": ("Tahun lunas melebihi masa pelindungan.", "Paid-through year exceeds the term."),
    "NEVER_VERIFIED": ("Data belum pernah diverifikasi terhadap sumber resmi.", "Data never verified against an official source."),
    "VERIFICATION_AGING": ("Verifikasi terakhir lebih dari 6 bulan lalu.", "Last verification over 6 months ago."),
    "VERIFICATION_STALE": ("Verifikasi terakhir lebih dari 12 bulan lalu.", "Last verification over 12 months ago."),
    "WORKING_STATEMENT_FINAL_YEAR_VERIFY": (
        "Perlu verifikasi apakah pernyataan pelaksanaan diwajibkan untuk tahun terakhir yang tidak penuh.",
        "Verify whether a working statement is required for the final partial year.",
    ),
    "APPLICATION_CODE_MISSING_ASSUMED_00": ("Kode '00' tidak ada pada nomor; diasumsikan '00'.", "Missing '00' code assumed."),
    "APPLICATION_CODE_UNUSUAL": ("Kode nomor permohonan tidak lazim.", "Unusual application number code."),
    "APPLICATION_YEAR_OUT_OF_RANGE": ("Tahun pada nomor permohonan di luar rentang wajar.", "Application year out of range."),
    "LEGACY_REGISTRATION_TYPE_UNKNOWN": ("Format nomor paten lama; jenis paten tidak diketahui.", "Legacy registration format; type unknown."),
    "REGISTRATION_ZERO_PADDED": ("Nomor paten dilengkapi dengan angka nol di depan.", "Registration number zero-padded."),
    "TYPE_CONFLICT": ("Jenis paten tidak konsisten antara nomor dan data.", "Patent type inconsistent between number and data."),
    "PCT_FILING_DATE_CHECK": (
        "Fase nasional PCT: pastikan tanggal penerimaan adalah tanggal pengajuan internasional.",
        "PCT national phase: ensure the filing date is the international filing date.",
    ),
}

_ALERT_TITLES: dict[AlertKind, tuple[str, str]] = {
    AlertKind.EXPIRY_APPROACHING: ("Pelindungan paten {number} berakhir pada {date}", "Patent {number} protection ends on {date}"),
    AlertKind.ANNUITY_DUE: ("Biaya tahunan paten {number} jatuh tempo {date}", "Annuity for patent {number} due {date}"),
    AlertKind.ANNUITY_GRACE: (
        "Biaya tahunan paten {number} dalam masa tenggang hingga {date} (dikenakan denda)",
        "Annuity for patent {number} in grace period until {date} (surcharge applies)",
    ),
    AlertKind.ANNUITY_PAYMENT_UNVERIFIED: (
        "Verifikasi pembayaran biaya tahunan paten {number} (batas masa tenggang {date})",
        "Verify annuity payment for patent {number} (grace ends {date})",
    ),
    AlertKind.WORKING_STATEMENT_DUE: (
        "Pernyataan pelaksanaan paten {number} jatuh tempo {date}",
        "Working Statement for patent {number} due {date}",
    ),
    AlertKind.WORKING_STATEMENT_OVERDUE: (
        "Pernyataan pelaksanaan paten {number} terlambat (jatuh tempo {date})",
        "Working Statement for patent {number} overdue (due {date})",
    ),
    AlertKind.STATUS_CHANGE: ("Status paten {number} berubah ({date})", "Status of patent {number} changed ({date})"),
    AlertKind.OWNERSHIP_CHANGE: ("Pemegang paten {number} berubah ({date})", "Owner of patent {number} changed ({date})"),
    AlertKind.LAPSE_RISK: (
        "Paten {number} berisiko dihapus: biaya tahunan tidak tercatat dibayar (masa tenggang berakhir {date})",
        "Patent {number} at risk of lapse: no recorded annuity payment (grace ended {date})",
    ),
}


def label(value: Enum | None, lang: str = "id") -> str:
    if value is None:
        return "-"
    pair = _LABELS.get(value)
    if pair is None:
        return str(getattr(value, "value", value))
    return pair[0] if lang == "id" else pair[1]


def warning_text(code: str, lang: str = "id") -> str:
    pair = _WARNINGS.get(code)
    if pair is None:
        return code
    return pair[0] if lang == "id" else pair[1]


def alert_title(kind: AlertKind, number: str, date_text: str, lang: str = "id") -> str:
    template = _ALERT_TITLES[kind][0 if lang == "id" else 1]
    return template.format(number=number, date=date_text)
