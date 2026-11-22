"""Parsing and normalization of Indonesian patent numbers.

Supported shapes (separators such as spaces, hyphens and dots are ignored):

=====================  ==========================  ===============================
Input example          Canonical form              Meaning
=====================  ==========================  ===============================
``P00201912345``       ``P00201912345``            Paten application (permohonan)
``S-00 2020 00123``    ``S00202000123``            Paten Sederhana application
``W00200501234``       ``W00200501234``            PCT national phase (historic W prefix)
``IDP 000 012 345``    ``IDP000012345``            Granted Paten (nomor paten)
``IDS000001234``       ``IDS000001234``            Granted Paten Sederhana
``ID 0 012 345``       ``ID0012345``               Legacy registration (type unknown)
``PCT/US2019/012345``  ``PCT/US2019/012345``       PCT international application
``WO 2019/123456``     ``WO2019/123456``           PCT publication
=====================  ==========================  ===============================

Registration numbers shorter than nine digits are zero-padded with a warning,
because spreadsheets routinely drop the leading zeros.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .enums import NumberKind, PatentType

_SEPARATORS = re.compile(r"[\s\-._]+")
_APPLICATION = re.compile(r"^(?P<prefix>[PSW])(?P<code>\d{2})?(?P<year>(?:19|20)\d{2})(?P<serial>\d{5})$")
_REGISTRATION = re.compile(r"^ID(?P<kind>[PS])?(?P<digits>\d{1,9})$")
_PCT_APPLICATION = re.compile(r"^PCT/?(?P<cc>[A-Z]{2})(?P<year>\d{4}|\d{2})/?(?P<serial>\d{1,6})$")
_PCT_PUBLICATION = re.compile(r"^WO/?(?P<year>\d{4})/?(?P<serial>\d{6})$")

_PREFIX_TYPE = {"P": PatentType.PATEN, "S": PatentType.PATEN_SEDERHANA, "W": PatentType.PATEN}

FIRST_FILING_YEAR = 1991  # Law No. 6/1989 on Patents took effect 1 August 1991


class PatentNumberError(ValueError):
    """Raised when input cannot be interpreted as any supported number format."""


@dataclass(frozen=True)
class PatentNumber:
    raw: str
    kind: NumberKind
    normalized: str
    patent_type: PatentType | None = None
    is_pct_national_phase: bool = False
    year: int | None = None
    warnings: tuple[str, ...] = field(default=())

    @property
    def is_indonesian(self) -> bool:
        return self.kind in (NumberKind.APPLICATION, NumberKind.REGISTRATION)


def _clean(raw: str) -> str:
    return _SEPARATORS.sub("", raw.strip().upper())


def parse_patent_number(raw: str, *, today: date | None = None) -> PatentNumber:
    if raw is None or not str(raw).strip():
        raise PatentNumberError("Nomor kosong / empty number")
    raw = str(raw)
    max_year = (today or date.today()).year + 1
    compact = _clean(raw)
    pct_compact = re.sub(r"\s+", "", raw.strip().upper())

    if m := _PCT_APPLICATION.match(pct_compact):
        year, serial = m["year"], m["serial"]
        width = 6 if len(year) == 4 else 5
        if len(serial) > width:
            raise PatentNumberError(f"Nomor PCT tidak valid: {raw!r}")
        normalized = f"PCT/{m['cc']}{year}/{serial.zfill(width)}"
        return PatentNumber(raw, NumberKind.PCT_APPLICATION, normalized, year=_full_year(year))

    if m := _PCT_PUBLICATION.match(pct_compact):
        return PatentNumber(
            raw, NumberKind.PCT_PUBLICATION, f"WO{m['year']}/{m['serial']}", year=int(m["year"])
        )

    if m := _APPLICATION.match(compact):
        warnings: list[str] = []
        code = m["code"]
        if code is None:
            warnings.append("APPLICATION_CODE_MISSING_ASSUMED_00")
            code = "00"
        elif code != "00":
            warnings.append("APPLICATION_CODE_UNUSUAL")
        year = int(m["year"])
        if not FIRST_FILING_YEAR <= year <= max_year:
            warnings.append("APPLICATION_YEAR_OUT_OF_RANGE")
        prefix = m["prefix"]
        return PatentNumber(
            raw,
            NumberKind.APPLICATION,
            f"{prefix}{code}{year}{m['serial']}",
            patent_type=_PREFIX_TYPE[prefix],
            is_pct_national_phase=prefix == "W",
            year=year,
            warnings=tuple(warnings),
        )

    if m := _REGISTRATION.match(compact):
        digits = m["digits"]
        kind = m["kind"]
        if kind is None:
            return PatentNumber(
                raw,
                NumberKind.REGISTRATION,
                f"ID{digits.zfill(7)}",
                warnings=("LEGACY_REGISTRATION_TYPE_UNKNOWN",),
            )
        warnings = ("REGISTRATION_ZERO_PADDED",) if len(digits) < 9 else ()
        return PatentNumber(
            raw,
            NumberKind.REGISTRATION,
            f"ID{kind}{digits.zfill(9)}",
            patent_type=PatentType.PATEN if kind == "P" else PatentType.PATEN_SEDERHANA,
            warnings=warnings,
        )

    raise PatentNumberError(f"Format nomor paten tidak dikenali / unrecognised number format: {raw!r}")


def try_parse_patent_number(raw: str | None, *, today: date | None = None) -> PatentNumber | None:
    try:
        return parse_patent_number(raw or "", today=today)
    except PatentNumberError:
        return None


def _full_year(year: str) -> int:
    if len(year) == 4:
        return int(year)
    two = int(year)
    return 1900 + two if two >= 78 else 2000 + two  # PCT started in 1978


def type_conflict(a: PatentType | None, b: PatentType | None) -> bool:
    return a is not None and b is not None and a != b
