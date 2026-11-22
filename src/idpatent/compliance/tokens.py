"""HMAC-signed tokens for one-click opt-out and preference-centre links.

Opt-out links must keep working (UU ITE requires a functional opt-out), so
they carry no expiry by default; preference links can be given a TTL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class InvalidToken(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict, key: bytes, ttl_seconds: int | None = None) -> str:
    body = dict(payload)
    body["v"] = 1
    if ttl_seconds is not None:
        body["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(key, raw, hashlib.sha256).digest()[:20]
    return f"{_b64(raw)}.{_b64(mac)}"


def verify(token: str, key: bytes) -> dict:
    try:
        raw_b64, mac_b64 = token.split(".")
        raw = _unb64(raw_b64)
        mac = _unb64(mac_b64)
    except (ValueError, TypeError) as exc:
        raise InvalidToken("malformed") from exc
    expected = hmac.new(key, raw, hashlib.sha256).digest()[:20]
    if not hmac.compare_digest(mac, expected):
        raise InvalidToken("bad signature")
    body = json.loads(raw)
    if "exp" in body and body["exp"] < time.time():
        raise InvalidToken("expired")
    return body
