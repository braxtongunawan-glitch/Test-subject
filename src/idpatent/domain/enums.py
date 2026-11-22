"""Enumerations shared across the system.

Codes are English identifiers (stable, used in the database and API); the
Bahasa Indonesia / English display labels live in :mod:`idpatent.domain.i18n`.
"""

from __future__ import annotations

from enum import Enum


class PatentType(str, Enum):
    PATEN = "PATEN"  # standard patent, 20 years
    PATEN_SEDERHANA = "PATEN_SEDERHANA"  # simple patent, 10 years


class NumberKind(str, Enum):
    APPLICATION = "APPLICATION"  # nomor permohonan, e.g. P00201912345
    REGISTRATION = "REGISTRATION"  # nomor paten / pendaftaran, e.g. IDP000012345
    PCT_APPLICATION = "PCT_APPLICATION"  # PCT/US2019/012345
    PCT_PUBLICATION = "PCT_PUBLICATION"  # WO2019/123456


class LegalStatus(str, Enum):
    PENDING = "PENDING"  # Dalam Proses
    GRANTED = "GRANTED"  # Diberi
    EXPIRED = "EXPIRED"  # Berakhir
    LAPSED = "LAPSED"  # Dihapus (e.g. unpaid annuity) – official record only
    LAPSE_SUSPECTED = "LAPSE_SUSPECTED"  # computed: annuity grace period passed unpaid
    REFUSED = "REFUSED"  # Ditolak
    WITHDRAWN = "WITHDRAWN"  # Ditarik Kembali / Dianggap Ditarik Kembali
    UNKNOWN = "UNKNOWN"


TERMINAL_RECORDED_STATUSES = frozenset({LegalStatus.LAPSED, LegalStatus.REFUSED, LegalStatus.WITHDRAWN})
INACTIVE_STATUSES = TERMINAL_RECORDED_STATUSES | {LegalStatus.EXPIRED}


class DataSource(str, Enum):
    PDKI = "PDKI"  # Pangkalan Data Kekayaan Intelektual public search extract
    SAKI = "SAKI"  # paten.dgip.go.id online system
    MANUAL_VERIFIED = "MANUAL_VERIFIED"  # verified by staff against an official record
    AGENT_FEED = "AGENT_FEED"  # authorized konsultan HKI / annuity provider feed
    USER_INPUT = "USER_INPUT"  # uploaded or typed by a user, unverified
    SUPPLEMENTARY = "SUPPLEMENTARY"  # EPO OPS / Google Patents (family / priority only)


class VerificationStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    OVERRIDDEN = "OVERRIDDEN"  # manual override in force


class InstallmentStatus(str, Enum):
    PAID = "PAID"
    UPCOMING = "UPCOMING"
    IN_GRACE = "IN_GRACE"  # past due date, within the surcharge grace period
    MISSED = "MISSED"  # grace period ended without a recorded payment
    UNKNOWN = "UNKNOWN"  # past due date, payment status not known


class ObligationStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    UPCOMING = "UPCOMING"
    OVERDUE = "OVERDUE"


class WorkingStatementMode(str, Enum):
    CALENDAR_YEAR_END = "CALENDAR_YEAR_END"  # one statement per calendar year, due 31 December
    WITH_ANNUITY = "WITH_ANNUITY"  # due together with each annuity installment


class TermEndConvention(str, Enum):
    ANNIVERSARY = "ANNIVERSARY"  # end date = filing date + N years
    DAY_BEFORE_ANNIVERSARY = "DAY_BEFORE_ANNIVERSARY"


class OwnerType(str, Enum):
    COMPANY = "COMPANY"
    UNIVERSITY = "UNIVERSITY"
    RESEARCH_GOVERNMENT = "RESEARCH_GOVERNMENT"
    INDIVIDUAL = "INDIVIDUAL"
    FOREIGN = "FOREIGN"
    UNKNOWN = "UNKNOWN"


class ConfidenceBand(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AlertKind(str, Enum):
    EXPIRY_APPROACHING = "EXPIRY_APPROACHING"
    ANNUITY_DUE = "ANNUITY_DUE"
    ANNUITY_GRACE = "ANNUITY_GRACE"
    ANNUITY_PAYMENT_UNVERIFIED = "ANNUITY_PAYMENT_UNVERIFIED"
    WORKING_STATEMENT_DUE = "WORKING_STATEMENT_DUE"
    WORKING_STATEMENT_OVERDUE = "WORKING_STATEMENT_OVERDUE"
    STATUS_CHANGE = "STATUS_CHANGE"
    OWNERSHIP_CHANGE = "OWNERSHIP_CHANGE"
    LAPSE_RISK = "LAPSE_RISK"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Purpose(str, Enum):
    """Purpose of an outbound communication (also the consent / opt-out granularity)."""

    ANNUITY_REMINDER = "ANNUITY_REMINDER"
    WORKING_STATEMENT_REMINDER = "WORKING_STATEMENT_REMINDER"
    EXPIRY_NOTICE = "EXPIRY_NOTICE"
    STATUS_NOTICE = "STATUS_NOTICE"
    COMMERCIAL_OFFER = "COMMERCIAL_OFFER"


PATENT_SPECIFIC_PURPOSES = frozenset(
    {
        Purpose.ANNUITY_REMINDER,
        Purpose.WORKING_STATEMENT_REMINDER,
        Purpose.EXPIRY_NOTICE,
        Purpose.STATUS_NOTICE,
    }
)


class LawfulBasis(str, Enum):
    """Legal bases for processing under UU No. 27/2022 (UU PDP) Art. 20(2)."""

    CONSENT = "CONSENT"
    CONTRACT = "CONTRACT"
    LEGAL_OBLIGATION = "LEGAL_OBLIGATION"
    LEGITIMATE_INTEREST = "LEGITIMATE_INTEREST"


class Relationship(str, Enum):
    CLIENT = "CLIENT"  # existing client relationship / engagement
    PROSPECT = "PROSPECT"  # no relationship: any message is "cold"
    AGENT = "AGENT"  # konsultan HKI / local representative
    OTHER = "OTHER"


class Role(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    FIRM_ADMIN = "FIRM_ADMIN"
    ACCOUNT_MANAGER = "ACCOUNT_MANAGER"
    VIEWER = "VIEWER"
    CLIENT_USER = "CLIENT_USER"


class AgreementKind(str, Enum):
    TERMS_OF_SERVICE = "TERMS_OF_SERVICE"  # Syarat dan Ketentuan Penggunaan
    PRIVACY_POLICY = "PRIVACY_POLICY"  # Kebijakan Privasi
    DATA_PROCESSING_NOTICE = "DATA_PROCESSING_NOTICE"  # third-party owner data authorization
    MARKETING_CONSENT = "MARKETING_CONSENT"  # separate, optional
    CLIENT_SERVICE_AGREEMENT = "CLIENT_SERVICE_AGREEMENT"


class Language(str, Enum):
    ID = "id"
    EN = "en"
