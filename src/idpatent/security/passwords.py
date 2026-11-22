"""Password hashing (scrypt), complexity and history policy (SRS 10.2)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
MIN_LENGTH = 12
HISTORY_SIZE = 5

_COMMON = {
    "password1234", "passw0rd1234", "123456789012", "qwertyuiop12", "indonesia123", "indonesia2024",
    "jakarta12345", "admin1234567", "welcome12345", "rahasia12345", "katasandi123",
}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    expected = base64.b64decode(hash_b64)
    actual = hashlib.scrypt(
        password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=len(expected)
    )
    return hmac.compare_digest(expected, actual)


def complexity_errors(password: str, *, email: str | None = None) -> list[str]:
    errors = []
    if len(password) < MIN_LENGTH:
        errors.append("PASSWORD_TOO_SHORT")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        errors.append("PASSWORD_NEEDS_3_CHARACTER_CLASSES")
    if password.lower() in _COMMON:
        errors.append("PASSWORD_TOO_COMMON")
    if email:
        local = email.split("@")[0].lower()
        if len(local) >= 4 and local in password.lower():
            errors.append("PASSWORD_CONTAINS_EMAIL")
    return errors


def reused(password: str, history: list[str]) -> bool:
    return any(verify_password(password, h) for h in history[-HISTORY_SIZE:])
