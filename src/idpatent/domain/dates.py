"""Date arithmetic and Indonesian formatting conventions.

* Western Indonesia Time (WIB) is UTC+7 with no daylight saving, so a fixed
  offset is exact and avoids a tz-database dependency.
* Anniversaries of 29 February fall on 28 February in non-leap years.
* Month arithmetic clamps to the last day of the target month
  (31 August + 6 months = 28/29 February).
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

WIB = timezone(timedelta(hours=7), "WIB")

MONTHS_ID = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]
MONTHS_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

_MONTH_LOOKUP: dict[str, int] = {}
for _i, (_id, _en) in enumerate(zip(MONTHS_ID, MONTHS_EN), start=1):
    for _name in (_id, _en):
        _MONTH_LOOKUP[_name.lower()] = _i
        _MONTH_LOOKUP[_name[:3].lower()] = _i
_MONTH_LOOKUP.update({"agt": 8, "agu": 8, "okt": 10, "des": 12, "mei": 5, "sept": 9, "nop": 11, "nopember": 11})


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February -> 28 February in a non-leap year
        return d.replace(year=d.year + years, day=28)


def add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def anniversaries_passed(start: date, on: date) -> int:
    """Number of anniversaries of ``start`` that fall on or before ``on``."""
    n = on.year - start.year
    if add_years(start, n) > on:
        n -= 1
    return n


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_wib() -> date:
    return datetime.now(WIB).date()


def to_wib(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(WIB)


def format_date(d: date | None, lang: str = "id") -> str:
    if d is None:
        return "-"
    months = MONTHS_ID if lang == "id" else MONTHS_EN
    return f"{d.day} {months[d.month - 1]} {d.year}"


def format_datetime_wib(dt: datetime | None, lang: str = "id") -> str:
    if dt is None:
        return "-"
    local = to_wib(dt)
    return f"{format_date(local.date(), lang)} {local:%H:%M} WIB"


def format_idr(amount: int | float | Decimal) -> str:
    """Rupiah, Indonesian convention: ``Rp1.500.000`` (dot thousands separator)."""
    value = int(Decimal(str(amount)).quantize(Decimal(1)))
    sign = "-" if value < 0 else ""
    return f"{sign}Rp{abs(value):,}".replace(",", ".")


_NUMERIC_DATE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")
_TEXT_DATE = re.compile(r"^(\d{1,2})[\s\-]+([A-Za-z]+)\.?[\s\-]+(\d{4})$")
_EXCEL_EPOCH = date(1899, 12, 30)


def parse_date(value: object) -> date | None:
    """Parse the date shapes found in Indonesian portfolio spreadsheets.

    Numeric dates are read day-first (``15/03/2019``), the Indonesian convention.
    Returns ``None`` for blank input and raises ``ValueError`` when unparseable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel serial date (1900 date system)
        if 1 <= value < 120000:
            return _EXCEL_EPOCH + timedelta(days=int(value))
        raise ValueError(f"Nilai tanggal tidak valid: {value!r}")
    text = str(value).strip()
    if not text:
        return None
    if m := _ISO_DATE.match(text):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := _NUMERIC_DATE.match(text):
        return date(int(m[3]), int(m[2]), int(m[1]))
    if m := _TEXT_DATE.match(text):
        month = _MONTH_LOOKUP.get(m[2].lower())
        if month:
            return date(int(m[3]), month, int(m[1]))
    raise ValueError(f"Format tanggal tidak dikenali: {text!r}")
