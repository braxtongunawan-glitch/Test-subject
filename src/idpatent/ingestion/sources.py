"""Data-source adapters (SRS 3.2 "Data sources").

There is no public machine-readable DGIP API comparable to USPTO ODP, so the
system is designed around *assisted* and *verified* data rather than scraping:

* :class:`PdkiAssistedLookup` builds the PDKI search link for an operator and
  turns the fields they confirm on the official page into a PDKI-sourced record
  with a retrieval date. It never fetches PDKI itself; automated extraction
  should only be added after confirming DGIP's terms of use, and must be rate
  limited.
* :class:`VerifiedDatasetSource` serves records from a verified upload
  (for example an annuity provider's or konsultan HKI's confirmed export).
* :class:`HttpJsonSource` reads an *authorized* JSON feed (agent feed, or a
  future official DGIP API) with an API key and a minimum request interval.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Callable, Protocol
from urllib.parse import quote

import httpx

from ..domain.enums import DataSource, NumberKind
from ..domain.numbers import PatentNumber, try_parse_patent_number
from .parsers import rows_to_records
from .records import PatentRecord

PDKI_SEARCH_URL = "https://pdki-indonesia.dgip.go.id/search?type=patent&keyword={query}"


class PatentDataSource(Protocol):
    name: DataSource

    def lookup(self, number: PatentNumber) -> PatentRecord | None: ...


class PdkiAssistedLookup:
    name = DataSource.PDKI

    def __init__(self, search_url_template: str = PDKI_SEARCH_URL):
        self.template = search_url_template

    def search_url(self, number: PatentNumber) -> str:
        return self.template.format(query=quote(number.normalized, safe=""))

    def lookup(self, number: PatentNumber) -> PatentRecord | None:
        return None  # operator-assisted: see record_from_operator

    def record_from_operator(self, fields: dict, *, retrieved_on: date) -> PatentRecord:
        """Build a record from values an operator copied from the official PDKI page."""
        records, errors = rows_to_records([fields], DataSource.PDKI, first_row_number=1)
        if errors:
            raise ValueError("; ".join(f"{e.field}: {e.message}" for e in errors))
        rec = records[0]
        rec.verified_on = retrieved_on
        if not rec.source_reference:
            n = try_parse_patent_number(rec.registration_number or rec.application_number)
            rec.source_reference = self.search_url(n) if n else None
        return rec


class VerifiedDatasetSource:
    name = DataSource.MANUAL_VERIFIED

    def __init__(self, records: list[PatentRecord]):
        self._index: dict[str, PatentRecord] = {}
        for rec in records:
            for raw in (rec.application_number, rec.registration_number):
                n = try_parse_patent_number(raw)
                if n:
                    self._index[n.normalized] = rec

    def lookup(self, number: PatentNumber) -> PatentRecord | None:
        return self._index.get(number.normalized)


class HttpJsonSource:
    """Authorized JSON feed: ``GET {base_url}/{normalized_number}`` returning one record object."""

    name = DataSource.AGENT_FEED

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        min_interval_seconds: float = 1.0,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not base_url.startswith("https://"):
            raise ValueError("Feeds must use HTTPS")
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json", "User-Agent": "idpatent/0.1"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = client or httpx.Client(timeout=20, headers=headers)
        if client is not None:
            self.client.headers.update(headers)
        self.min_interval = min_interval_seconds
        self._clock, self._sleep = clock, sleep
        self._last = None

    def lookup(self, number: PatentNumber) -> PatentRecord | None:
        if number.kind not in (NumberKind.APPLICATION, NumberKind.REGISTRATION):
            return None
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        resp = self.client.get(f"{self.base_url}/{quote(number.normalized, safe='')}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        records, errors = rows_to_records([resp.json()], DataSource.AGENT_FEED, first_row_number=1)
        if errors:
            raise ValueError("; ".join(f"{e.field}: {e.message}" for e in errors))
        rec = records[0]
        rec.verified_on = rec.verified_on or date.today()
        rec.source_reference = rec.source_reference or str(resp.url)
        return rec
