import json
from datetime import date

import httpx
import pytest

from idpatent.cli import main
from idpatent.domain.enums import DataSource, LegalStatus, PatentType
from idpatent.domain.numbers import parse_patent_number
from idpatent.ingestion.parsers import parse_priority, read_rows, rows_to_records
from idpatent.ingestion.records import parse_patent_type, parse_status
from idpatent.ingestion.sources import HttpJsonSource, PdkiAssistedLookup, VerifiedDatasetSource


def test_status_and_type_text():
    assert parse_status("Dianggap Ditarik Kembali") == LegalStatus.WITHDRAWN
    assert parse_status("Diberi Paten") == LegalStatus.GRANTED
    assert parse_status("Berakhir") == LegalStatus.EXPIRED
    assert parse_status("aneh") == LegalStatus.UNKNOWN
    assert parse_patent_type("Paten Sederhana") == PatentType.PATEN_SEDERHANA
    assert parse_patent_type("utility model") == PatentType.PATEN_SEDERHANA
    with pytest.raises(ValueError):
        parse_patent_type("merek")


def test_priority_parsing():
    claims = parse_priority("US 62/123,456 2019-01-15; JP 2019-001234 01/02/2019")
    assert claims == [
        {"country": "US", "number": "62/123,456", "date": "2019-01-15"},
        {"country": "JP", "number": "2019-001234", "date": "2019-02-01"},
    ]


def test_json_and_header_aliases():
    rows = read_rows("x.json", json.dumps({"paten": [
        {"Nomor Permohonan": "P00201912345", "Tanggal Penerimaan": "01-02-2019", "Pemegang Paten": "PT A",
         "Hak Prioritas": "SG 10201801234X 2018-02-01"}]}).encode())
    records, errors = rows_to_records(rows, DataSource.USER_INPUT)
    assert not errors
    r = records[0]
    assert r.filing_date == date(2019, 2, 1) and r.owner_name == "PT A" and r.priority_claims[0]["country"] == "SG"


def test_pdki_assisted_lookup():
    lookup = PdkiAssistedLookup()
    n = parse_patent_number("IDP000012345")
    assert lookup.search_url(n).endswith("keyword=IDP000012345")
    rec = lookup.record_from_operator({"nomor_paten": "IDP000012345", "tanggal_penerimaan": "10/03/2015",
                                       "status": "Diberi"}, retrieved_on=date(2026, 11, 22))
    assert rec.source == DataSource.PDKI and rec.verified_on == date(2026, 11, 22)
    assert rec.source_reference.startswith("https://pdki-indonesia.dgip.go.id/")
    with pytest.raises(ValueError):
        lookup.record_from_operator({"nomor_paten": "IDP000012345", "tanggal_penerimaan": "besok"}, retrieved_on=date.today())


def test_verified_dataset_source():
    records, _ = rows_to_records([{"nomor_permohonan": "P00201912345", "nomor_paten": "IDP000000777"}], DataSource.MANUAL_VERIFIED)
    src = VerifiedDatasetSource(records)
    assert src.lookup(parse_patent_number("IDP777")) is records[0]


def test_http_json_source_rate_limit_and_auth():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("P00201900001"):
            return httpx.Response(404)
        return httpx.Response(200, json={"application_number": "P00201912345", "filing_date": "2019-03-01",
                                         "status": "Granted", "grant_date": "2022-01-05"})

    sleeps = []
    t = [100.0]
    src = HttpJsonSource("https://feed.example.id/patents", "secret-key", min_interval_seconds=2.0,
                         client=httpx.Client(transport=httpx.MockTransport(handler)),
                         clock=lambda: t[0], sleep=sleeps.append)
    rec = src.lookup(parse_patent_number("P00201912345"))
    assert rec.source == DataSource.AGENT_FEED and rec.recorded_status == LegalStatus.GRANTED
    assert src.lookup(parse_patent_number("P00201900001")) is None
    assert sleeps == [2.0]
    assert seen[0].headers["authorization"] == "Bearer secret-key"
    assert src.lookup(parse_patent_number("PCT/US2019/012345")) is None
    with pytest.raises(ValueError):
        HttpJsonSource("http://insecure.example")


def test_cli_gen_keys_and_init(tmp_path, monkeypatch, capsys):
    assert main(["gen-keys"]) == 0
    keys = dict(line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines())
    for k, v in keys.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("IDPATENT_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("IDPATENT_ADMIN_PASSWORD", "Rahasia#Kuat2026")
    assert main(["init", "--admin-email", "root@contoh.co.id", "--admin-name", "Root"]) == 0
    assert main(["run-alerts", "--as-of", "2026-11-22"]) == 0
    assert main(["verify-audit"]) == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out
