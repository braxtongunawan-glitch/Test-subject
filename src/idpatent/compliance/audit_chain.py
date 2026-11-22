"""Tamper-evident hash chain for audit and acceptance records (SRS 10.4, 11.5).

Each entry stores ``prev_hash`` and ``entry_hash = SHA-256(prev_hash || canonical_json(payload))``.
Altering, inserting or deleting any historical row breaks every later hash,
which :func:`verify_chain` detects. Database triggers additionally reject
UPDATE/DELETE on the audit tables (see ``idpatent.db.session``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

GENESIS_HASH = "0" * 64


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def chain_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256(prev_hash.encode() + canonical(payload)).hexdigest()


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    checked: int
    first_broken_id: object | None = None


def verify_chain(entries: Iterable[tuple[object, dict, str, str]]) -> ChainVerification:
    """``entries`` yields ``(id, payload, prev_hash, entry_hash)`` in insertion order."""
    expected_prev = GENESIS_HASH
    count = 0
    for entry_id, payload, prev_hash, entry_hash in entries:
        count += 1
        if prev_hash != expected_prev or chain_hash(prev_hash, payload) != entry_hash:
            return ChainVerification(False, count, entry_id)
        expected_prev = entry_hash
    return ChainVerification(True, count)
