"""FastAPI dependencies: DB session, authentication, RBAC, agreement gating, language."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db.models import Firm, User
from ..domain.enums import Role
from ..security.rbac import Permission, has_permission
from ..services import agreements as agreement_service
from ..services import auth as auth_service
from ..services.audit import Actor
from ..services.context import AppContext

SESSION_COOKIE = "idp_session"
CSRF_HEADER = "x-requested-with"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_db(ctx: AppContext = Depends(get_ctx)) -> Iterator[Session]:
    """Request-scoped session. Endpoints commit explicitly; anything uncommitted is discarded."""
    session = ctx.sessions()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def api_error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message, **extra})


@dataclass
class Principal:
    user: User
    firm: Firm | None
    ip: str | None
    lang: str

    @property
    def role(self) -> Role:
        return Role(self.user.role)

    @property
    def actor(self) -> Actor:
        return Actor(self.user.id, f"user:{self.user.id}", self.user.firm_id, self.ip)

    @property
    def firm_id(self) -> int:
        if self.firm is None:
            raise api_error(400, "FIRM_CONTEXT_REQUIRED", "Pengguna ini tidak terkait dengan firma")
        return self.firm.id

    def can(self, permission: Permission) -> bool:
        return has_permission(self.role, permission)


def _token(request: Request) -> tuple[str | None, bool]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), False
    return request.cookies.get(SESSION_COOKIE), True


def current_principal(request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)) -> Principal:
    token, from_cookie = _token(request)
    if from_cookie and token and request.method not in SAFE_METHODS and not request.headers.get(CSRF_HEADER):
        raise api_error(403, "CSRF_HEADER_REQUIRED", "Header X-Requested-With wajib untuk permintaan berbasis cookie")
    try:
        user = auth_service.authenticate(db, ctx, token)
    except auth_service.AuthError as exc:
        db.commit()  # persist idle-timeout revocation
        raise api_error(exc.status, exc.code, str(exc)) from exc
    db.commit()
    firm = db.get(Firm, user.firm_id) if user.firm_id else None
    lang = request.query_params.get("lang") or user.language or "id"
    return Principal(user, firm, client_ip(request), "en" if lang == "en" else "id")


def require(*permissions: Permission):
    def dep(principal: Principal = Depends(current_principal)) -> Principal:
        missing = [p.value for p in permissions if not principal.can(p)]
        if missing:
            raise api_error(403, "FORBIDDEN", "Akses ditolak", missing=missing)
        return principal

    return dep


def require_agreements(principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Principal:
    pending = agreement_service.pending_documents(db, principal.user, principal.firm, principal.lang)
    if pending:
        raise api_error(
            403, "AGREEMENT_ACCEPTANCE_REQUIRED", "Setujui dokumen terbaru sebelum melanjutkan",
            pending=[{"document_id": d.id, "kind": d.kind, "version": d.version, "title": d.title} for d in pending],
        )
    return principal


def guarded(*permissions: Permission):
    """Permission check plus current-agreement check, for every data feature."""

    def dep(principal: Principal = Depends(require(*permissions)), _: Principal = Depends(require_agreements)) -> Principal:
        return principal

    return dep


class RateLimiter:
    """Small in-process sliding-window limiter for authentication endpoints.

    Production deployments should also rate limit at the WAF / reverse proxy.
    """

    def __init__(self, limit: int, window_seconds: float):
        self.limit, self.window = limit, window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise api_error(429, "RATE_LIMITED", "Terlalu banyak percobaan, coba lagi nanti")
        q.append(now)
