from datetime import date, datetime, timezone

import pytest

from idpatent.domain.dates import (
    add_months, add_years, anniversaries_passed, format_date, format_datetime_wib, format_idr, parse_date,
)
from idpatent.domain.enums import NumberKind, PatentType
from idpatent.domain.numbers import PatentNumberError, parse_patent_number, try_parse_patent_number


def test_add_years_leap_day():
    assert add_years(date(2016, 2, 29), 10) == date(2026, 2, 28)
    assert add_years(date(2016, 2, 29), 20) == date(2036, 2, 29)


def test_add_months_clamps():
    assert add_months(date(2025, 8, 31), 6) == date(2026, 2, 28)
    assert add_months(date(2027, 3, 31), -1) == date(2027, 2, 28)
    assert add_months(date(2026, 1, 15), -1) == date(2025, 12, 15)


def test_anniversaries_passed():
    assert anniversaries_passed(date(2015, 3, 10), date(2019, 3, 9)) == 3
    assert anniversaries_passed(date(2015, 3, 10), date(2019, 3, 10)) == 4


def test_indonesian_formatting():
    assert format_date(date(2030, 3, 15)) == "15 Maret 2030"
    assert format_date(date(2030, 3, 15), "en") == "15 March 2030"
    assert format_idr(1500000) == "Rp1.500.000"
    assert format_datetime_wib(datetime(2026, 1, 1, 17, 30, tzinfo=timezone.utc)) == "2 Januari 2026 00:30 WIB"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2019-03-15", date(2019, 3, 15)),
        ("15/03/2019", date(2019, 3, 15)),
        ("05-11-2019", date(2019, 11, 5)),
        ("15 Maret 2019", date(2019, 3, 15)),
        ("1 Agustus 2020", date(2020, 8, 1)),
        ("7 Des 2021", date(2021, 12, 7)),
        (43539, date(2019, 3, 15)),
        ("", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_parse_date_rejects_garbage():
    with pytest.raises(ValueError):
        parse_date("kemarin")


@pytest.mark.parametrize(
    "raw,normalized,kind,ptype,pct",
    [
        ("P00201912345", "P00201912345", NumberKind.APPLICATION, PatentType.PATEN, False),
        ("p-00 2019 12345", "P00201912345", NumberKind.APPLICATION, PatentType.PATEN, False),
        ("S00202000123", "S00202000123", NumberKind.APPLICATION, PatentType.PATEN_SEDERHANA, False),
        ("W00200501234", "W00200501234", NumberKind.APPLICATION, PatentType.PATEN, True),
        ("IDP 000 012 345", "IDP000012345", NumberKind.REGISTRATION, PatentType.PATEN, False),
        ("IDS000001234", "IDS000001234", NumberKind.REGISTRATION, PatentType.PATEN_SEDERHANA, False),
        ("PCT/US2019/012345", "PCT/US2019/012345", NumberKind.PCT_APPLICATION, None, False),
        ("pct/jp 2020/5", "PCT/JP2020/000005", NumberKind.PCT_APPLICATION, None, False),
        ("PCT/EP03/01234", "PCT/EP03/01234", NumberKind.PCT_APPLICATION, None, False),
        ("WO 2019/123456", "WO2019/123456", NumberKind.PCT_PUBLICATION, None, False),
    ],
)
def test_parse_numbers(raw, normalized, kind, ptype, pct):
    n = parse_patent_number(raw, today=date(2026, 11, 22))
    assert n.normalized == normalized
    assert n.kind == kind
    assert n.patent_type == ptype
    assert n.is_pct_national_phase == pct


def test_registration_zero_padding_warns():
    n = parse_patent_number("IDP12345")
    assert n.normalized == "IDP000012345"
    assert "REGISTRATION_ZERO_PADDED" in n.warnings


def test_legacy_registration():
    n = parse_patent_number("ID 0 012 345")
    assert n.normalized == "ID0012345"
    assert n.patent_type is None
    assert "LEGACY_REGISTRATION_TYPE_UNKNOWN" in n.warnings


def test_application_missing_code_and_year_range():
    n = parse_patent_number("P201912345", today=date(2026, 1, 1))
    assert n.normalized == "P00201912345"
    assert "APPLICATION_CODE_MISSING_ASSUMED_00" in n.warnings
    assert "APPLICATION_YEAR_OUT_OF_RANGE" in parse_patent_number("P00198812345").warnings


@pytest.mark.parametrize("raw", ["", "XYZ123", "IDP1234567890", "P0020191234"])
def test_invalid_numbers(raw):
    with pytest.raises(PatentNumberError):
        parse_patent_number(raw)
    assert try_parse_patent_number(raw) is None
