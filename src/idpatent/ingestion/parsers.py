"""Bulk upload parsing: CSV, Excel (.xlsx) and JSON with Indonesian/English headers."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass

from ..domain.dates import parse_date
from ..domain.enums import DataSource
from .records import PatentRecord, parse_patent_type, parse_status

MAX_ROWS = 20000

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "application_number": ("application_number", "nomor_permohonan", "no_permohonan", "application_no", "app_no", "nomor_aplikasi"),
    "registration_number": ("registration_number", "nomor_paten", "no_paten", "nomor_pendaftaran", "patent_number", "patent_no", "nomor_sertifikat", "nomor_registrasi"),
    "patent_type": ("patent_type", "jenis", "jenis_paten", "type", "tipe"),
    "title": ("title", "judul", "judul_invensi", "invention_title"),
    "filing_date": ("filing_date", "tanggal_penerimaan", "tgl_penerimaan", "tanggal_pengajuan", "tanggal_permohonan", "application_date"),
    "grant_date": ("grant_date", "tanggal_pemberian", "tgl_pemberian", "tanggal_sertifikat", "tanggal_diberi", "tanggal_pemberian_paten", "registration_date"),
    "status": ("status", "status_paten", "legal_status", "status_hukum"),
    "owner": ("owner", "pemegang", "pemegang_paten", "pemohon", "applicant", "assignee", "nama_pemegang", "pemilik"),
    "owner_country": ("owner_country", "negara", "country", "negara_pemohon", "negara_pemegang"),
    "paid_through_year": ("paid_through_year", "dibayar_sampai_tahun", "lunas_sampai_tahun", "annuity_paid_through", "tahun_lunas"),
    "pct_application_number": ("pct_number", "pct_application_number", "nomor_pct", "pct", "pct_application"),
    "pct_publication_number": ("pct_publication", "pct_publication_number", "wo_number", "nomor_publikasi_pct"),
    "priority": ("priority", "prioritas", "priority_claims", "hak_prioritas", "data_prioritas"),
    "agent": ("agent", "konsultan", "konsultan_hki", "kuasa", "agent_name"),
    "source_reference": ("source_reference", "referensi", "url", "sumber_url"),
    "verified_on": ("verified_on", "tanggal_verifikasi", "last_verified", "diverifikasi"),
}
_LOOKUP = {alias: field for field, aliases in HEADER_ALIASES.items() for alias in aliases}


def normalize_header(h: object) -> str:
    return re.sub(r"[\s\-./]+", "_", str(h or "").strip().lower()).strip("_")


@dataclass
class RowError:
    row: int
    field: str
    message: str


class ParseError(ValueError):
    pass


def read_rows(filename: str, content: bytes) -> list[dict]:
    name = filename.lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        rows = _read_csv(content)
    elif name.endswith(".xlsx") or name.endswith(".xlsm"):
        rows = _read_xlsx(content)
    elif name.endswith(".json"):
        rows = _read_json(content)
    else:
        raise ParseError("Format file tidak didukung (gunakan CSV, XLSX atau JSON)")
    if len(rows) > MAX_ROWS:
        raise ParseError(f"Terlalu banyak baris (maksimum {MAX_ROWS})")
    return rows


def _read_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]


def _read_xlsx(content: bytes) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    try:
        headers = [str(h) if h is not None else "" for h in next(it)]
    except StopIteration:
        return []
    rows = []
    for values in it:
        if values is None or all(v is None or str(v).strip() == "" for v in values):
            continue
        rows.append({headers[i]: v for i, v in enumerate(values) if i < len(headers)})
    wb.close()
    return rows


def _read_json(content: bytes) -> list[dict]:
    data = json.loads(content.decode("utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("patents") or data.get("paten") or data.get("data") or []
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ParseError("JSON harus berupa daftar objek paten")
    return data


def parse_priority(value: object) -> list[dict]:
    """``"US 62/123,456 2019-01-15; JP 2019-001234 01/02/2019"`` -> list of claims."""
    if value is None or not str(value).strip():
        return []
    if isinstance(value, list):
        return [dict(v) for v in value if isinstance(v, dict)]
    claims = []
    for part in str(value).split(";"):
        tokens = part.split()
        if len(tokens) < 2:
            continue
        country = tokens[0].upper() if re.fullmatch(r"[A-Za-z]{2}", tokens[0]) else None
        claim_date = None
        try:
            claim_date = parse_date(tokens[-1])
            number_tokens = tokens[1:-1] if country else tokens[:-1]
        except ValueError:
            number_tokens = tokens[1:] if country else tokens
        claims.append(
            {"country": country, "number": " ".join(number_tokens), "date": claim_date.isoformat() if claim_date else None}
        )
    return claims


def _clean(v: object) -> object:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def rows_to_records(rows: list[dict], source: DataSource, *, first_row_number: int = 2) -> tuple[list[PatentRecord], list[RowError]]:
    records: list[PatentRecord] = []
    errors: list[RowError] = []
    for offset, raw in enumerate(rows):
        row_no = first_row_number + offset
        row = {}
        for k, v in raw.items():
            field = _LOOKUP.get(normalize_header(k))
            if field:
                row[field] = _clean(v)
        rec = PatentRecord(source=source, row_number=row_no)
        ok = True

        def take(field: str, fn):
            nonlocal ok
            value = row.get(field)
            if value is None:
                return None
            try:
                return fn(value)
            except (ValueError, TypeError) as exc:
                errors.append(RowError(row_no, field, str(exc)))
                ok = False
                return None

        rec.application_number = row.get("application_number") and str(row["application_number"])
        rec.registration_number = row.get("registration_number") and str(row["registration_number"])
        rec.patent_type = take("patent_type", parse_patent_type)
        rec.title = row.get("title") and str(row["title"])
        rec.filing_date = take("filing_date", parse_date)
        rec.grant_date = take("grant_date", parse_date)
        rec.recorded_status = take("status", parse_status)
        rec.owner_name = row.get("owner") and str(row["owner"])
        rec.owner_country = take("owner_country", lambda v: str(v).strip().upper()[:2])
        rec.paid_through_year = take("paid_through_year", lambda v: int(float(v)))
        rec.pct_application_number = row.get("pct_application_number") and str(row["pct_application_number"])
        rec.pct_publication_number = row.get("pct_publication_number") and str(row["pct_publication_number"])
        rec.priority_claims = take("priority", parse_priority) or []
        rec.agent_name = row.get("agent") and str(row["agent"])
        rec.source_reference = row.get("source_reference") and str(row["source_reference"])
        rec.verified_on = take("verified_on", parse_date)
        if not rec.application_number and not rec.registration_number:
            errors.append(RowError(row_no, "application_number", "Nomor permohonan atau nomor paten wajib diisi"))
            ok = False
        if ok:
            records.append(rec)
    return records, errors
