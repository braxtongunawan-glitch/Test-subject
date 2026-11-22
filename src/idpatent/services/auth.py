"""Users, password login with mandatory TOTP MFA, lockout and server-side sessions (SRS 10.2)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import AuthSession, User, utcnow
from ..domain.enums import Role
from ..security import passwords, totp
from . import audit
from .audit import Actor
from .context import AppContext

_DUMMY_HASH = passwords.hash_password("dummy-password-for-timing-1!A")


class AuthError(ValueError):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code, self.status = code, status


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def find_user_by_email(session: Session, ctx: AppContext, email: str) -> User | None:
    return session.execute(select(User).where(User.email_index == ctx.cipher.blind_index(email, "user"))).scalar()


def user_email(ctx: AppContext, user: User) -> str:
    return ctx.cipher.decrypt(user.email_enc, "user.email")


def create_user(session: Session, ctx: AppContext, *, email: str, name: str, role: Role, password: str,
                firm_id: int | None, language: str = "id", owner_scope: list[int] | None = None,
                actor: Actor | None = None) -> User:
    email = email.strip().lower()
    if "@" not in email:
        raise AuthError("INVALID_EMAIL", "Email tidak valid", 422)
    if find_user_by_email(session, ctx, email):
        raise AuthError("USER_EXISTS", "Pengguna sudah terdaftar", 409)
    errors = passwords.complexity_errors(password, email=email)
    if errors:
        raise AuthError(",".join(errors), "Kata sandi tidak memenuhi kebijakan", 422)
    if role != Role.SUPER_ADMIN and firm_id is None:
        raise AuthError("FIRM_REQUIRED", "Pengguna harus terkait dengan firma", 422)
    hashed = passwords.hash_password(password)
    user = User(
        firm_id=firm_id, email_index=ctx.cipher.blind_index(email, "user"),
        email_enc=ctx.cipher.encrypt(email, "user.email"), name=name, role=role.value,
        language=language if language in ("id", "en") else "id", password_hash=hashed, password_history=[hashed],
        owner_scope=owner_scope or [],
    )
    session.add(user)
    session.flush()
    audit.record(session, actor or Actor.system("bootstrap"), "user.create", "user", user.id, {"role": role.value}, firm_id)
    return user


def _new_token(session: Session, user: User, purpose: str, lifetime: timedelta, ip: str | None, ua: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(AuthSession(token_hash=_hash_token(token), purpose=purpose, user_id=user.id, created_at=now,
                            last_seen_at=now, expires_at=now + lifetime, ip=ip, user_agent=(ua or "")[:400]))
    session.flush()
    return token


@dataclass
class LoginResult:
    status: str  # MFA_REQUIRED | MFA_ENROLLMENT_REQUIRED
    challenge: str


def login(session: Session, ctx: AppContext, email: str, password: str, ip: str | None, ua: str | None) -> LoginResult:
    user = find_user_by_email(session, ctx, email or "")
    now = utcnow()
    actor = Actor(user.id if user else None, f"login:{'known' if user else 'unknown'}", user.firm_id if user else None, ip)
    if user is None:
        passwords.verify_password(password or "", _DUMMY_HASH)  # equalize timing
        audit.record(session, actor, "auth.login_failed", details={"reason": "unknown_user"})
        raise AuthError("INVALID_CREDENTIALS", "Email atau kata sandi salah")
    if not user.is_active:
        raise AuthError("ACCOUNT_DISABLED", "Akun dinonaktifkan", 403)
    if user.locked_until and user.locked_until > now:
        audit.record(session, actor, "auth.login_locked", "user", user.id)
        raise AuthError("ACCOUNT_LOCKED", "Akun dikunci sementara karena terlalu banyak percobaan", 423)
    if not passwords.verify_password(password or "", user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= ctx.settings.lockout_threshold:
            user.locked_until = now + timedelta(minutes=ctx.settings.lockout_minutes)
            user.failed_logins = 0
            audit.record(session, actor, "auth.account_locked", "user", user.id)
        audit.record(session, actor, "auth.login_failed", "user", user.id, {"reason": "bad_password"})
        raise AuthError("INVALID_CREDENTIALS", "Email atau kata sandi salah")
    user.failed_logins = 0
    challenge = _new_token(session, user, "MFA_CHALLENGE", timedelta(minutes=ctx.settings.mfa_challenge_minutes), ip, ua)
    audit.record(session, actor, "auth.password_ok", "user", user.id)
    return LoginResult("MFA_REQUIRED" if user.mfa_enabled else "MFA_ENROLLMENT_REQUIRED", challenge)


def _challenge_user(session: Session, challenge: str) -> tuple[AuthSession, User]:
    row = session.execute(select(AuthSession).where(AuthSession.token_hash == _hash_token(challenge or ""),
                                                    AuthSession.purpose == "MFA_CHALLENGE")).scalar()
    if row is None or row.revoked_at or row.expires_at < utcnow():
        raise AuthError("CHALLENGE_INVALID", "Sesi verifikasi tidak valid atau kedaluwarsa")
    return row, row.user


def begin_mfa_enrollment(session: Session, ctx: AppContext, challenge: str) -> dict:
    _, user = _challenge_user(session, challenge)
    if user.mfa_enabled:
        raise AuthError("MFA_ALREADY_ENABLED", "MFA sudah aktif", 409)
    secret = totp.generate_secret()
    user.mfa_secret_enc = ctx.cipher.encrypt(secret, "user.mfa")
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, user_email(ctx, user), ctx.settings.issuer_name)}


def complete_mfa(session: Session, ctx: AppContext, challenge: str, code: str, ip: str | None, ua: str | None) -> str:
    """Verify the TOTP code (enrolling if needed) and exchange the challenge for a full session."""
    row, user = _challenge_user(session, challenge)
    if not user.mfa_secret_enc:
        raise AuthError("MFA_NOT_ENROLLED", "Mulai pendaftaran MFA terlebih dahulu", 409)
    secret = ctx.cipher.decrypt(user.mfa_secret_enc, "user.mfa")
    step = totp.verify(secret, code, last_used_step=user.mfa_last_step)
    actor = Actor(user.id, f"user:{user.id}", user.firm_id, ip)
    if step is None:
        user.failed_logins += 1
        if user.failed_logins >= ctx.settings.lockout_threshold:
            user.locked_until = utcnow() + timedelta(minutes=ctx.settings.lockout_minutes)
            user.failed_logins = 0
            row.revoked_at = utcnow()
        audit.record(session, actor, "auth.mfa_failed", "user", user.id)
        raise AuthError("MFA_INVALID", "Kode verifikasi salah")
    enrolled_now = not user.mfa_enabled
    user.mfa_enabled = True
    user.mfa_last_step = step
    user.failed_logins = 0
    user.last_login_at = utcnow()
    row.revoked_at = utcnow()
    token = _new_token(session, user, "SESSION", timedelta(hours=ctx.settings.session_absolute_hours), ip, ua)
    audit.record(session, actor, "auth.mfa_enrolled" if enrolled_now else "auth.login", "user", user.id)
    return token


def authenticate(session: Session, ctx: AppContext, token: str | None) -> User:
    if not token:
        raise AuthError("NOT_AUTHENTICATED", "Autentikasi diperlukan")
    row = session.execute(select(AuthSession).where(AuthSession.token_hash == _hash_token(token),
                                                    AuthSession.purpose == "SESSION")).scalar()
    now = utcnow()
    if row is None or row.revoked_at is not None or row.expires_at < now:
        raise AuthError("SESSION_INVALID", "Sesi tidak valid atau kedaluwarsa")
    if row.last_seen_at + timedelta(minutes=ctx.settings.session_idle_minutes) < now:
        row.revoked_at = now
        raise AuthError("SESSION_IDLE_TIMEOUT", "Sesi berakhir karena tidak aktif")
    user = row.user
    if not user.is_active or (user.locked_until and user.locked_until > now):
        raise AuthError("ACCOUNT_DISABLED", "Akun tidak aktif", 403)
    row.last_seen_at = now
    return user


def logout(session: Session, token: str | None) -> None:
    if not token:
        return
    row = session.execute(select(AuthSession).where(AuthSession.token_hash == _hash_token(token))).scalar()
    if row and row.revoked_at is None:
        row.revoked_at = utcnow()


def change_password(session: Session, ctx: AppContext, user: User, old: str, new: str) -> None:
    if not passwords.verify_password(old, user.password_hash):
        raise AuthError("INVALID_CREDENTIALS", "Kata sandi lama salah")
    errors = passwords.complexity_errors(new, email=user_email(ctx, user))
    if errors:
        raise AuthError(",".join(errors), "Kata sandi tidak memenuhi kebijakan", 422)
    if passwords.reused(new, user.password_history or []):
        raise AuthError("PASSWORD_REUSED", "Kata sandi pernah digunakan sebelumnya", 422)
    hashed = passwords.hash_password(new)
    user.password_hash = hashed
    user.password_history = (list(user.password_history or []) + [hashed])[-passwords.HISTORY_SIZE:]
    user.password_changed_at = utcnow()
    # revoke other sessions
    for s in session.execute(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))).scalars():
        s.revoked_at = utcnow()
    audit.record(session, Actor(user.id, f"user:{user.id}", user.firm_id), "auth.password_changed", "user", user.id)
