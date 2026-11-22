"""Field-level encryption for personal data (SRS 10.3).

* AES-256-GCM with a random 96-bit nonce per value; the column name is bound
  as associated data so ciphertext cannot be swapped between fields.
* A keyed HMAC-SHA256 "blind index" lets the system look up and de-duplicate
  e-mail addresses (suppression list, contact matching) without storing them
  in clear text.
* Keys come from a :class:`KeyProvider`; the default reads environment
  variables. Production deployments should implement the provider against a
  KMS/HSM so keys are never stored with the data.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "v1:"


class KeyProvider(Protocol):
    def data_key(self) -> bytes: ...
    def index_key(self) -> bytes: ...


class StaticKeyProvider:
    def __init__(self, data_key: bytes, index_key: bytes):
        if len(data_key) != 32 or len(index_key) != 32:
            raise ValueError("Keys must be 32 bytes (AES-256 / HMAC-SHA256)")
        self._data, self._index = data_key, index_key

    def data_key(self) -> bytes:
        return self._data

    def index_key(self) -> bytes:
        return self._index

    @classmethod
    def from_b64(cls, data_key_b64: str, index_key_b64: str) -> "StaticKeyProvider":
        return cls(base64.b64decode(data_key_b64), base64.b64decode(index_key_b64))

    @classmethod
    def ephemeral(cls) -> "StaticKeyProvider":
        return cls(os.urandom(32), os.urandom(32))


def generate_key_b64() -> str:
    return base64.b64encode(os.urandom(32)).decode()


class FieldCipher:
    def __init__(self, keys: KeyProvider):
        self._aead = AESGCM(keys.data_key())
        self._index_key = keys.index_key()

    def encrypt(self, plaintext: str | None, context: str) -> str | None:
        if plaintext is None:
            return None
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), context.encode())
        return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode()

    def decrypt(self, token: str | None, context: str) -> str | None:
        if token is None:
            return None
        if not token.startswith(PREFIX):
            raise ValueError("Unknown ciphertext version")
        raw = base64.urlsafe_b64decode(token[len(PREFIX):])
        return self._aead.decrypt(raw[:12], raw[12:], context.encode()).decode("utf-8")

    def blind_index(self, value: str, context: str = "email") -> str:
        normalized = value.strip().lower()
        return hmac.new(self._index_key, f"{context}:{normalized}".encode(), hashlib.sha256).hexdigest()


def mask_email(email: str) -> str:
    """``budi.santoso@contoh.co.id`` -> ``b***o@contoh.co.id`` for low-privilege views."""
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"
