"""Authentication, MFA, session and click-wrap agreement endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...db.models import AgreementDocument
from ...domain.enums import AgreementKind
from ...security.rbac import ROLE_PERMISSIONS
from ...services import agreements as agreement_service
from ...services import auth as auth_service
from ...services.context import AppContext
from ..deps import (
    SESSION_COOKIE, Principal, RateLimiter, api_error, client_ip, current_principal, get_ctx, get_db,
)

router = APIRouter(tags=["auth"])


def _limiter(request: Request, name: str) -> RateLimiter:
    """Per-application limiter (kept on app.state so separate app instances do not share counters)."""
    limiters = request.app.state.__dict__.setdefault("_rate_limiters", {})
    return limiters.setdefault(name, RateLimiter(limit=10, window_seconds=60))


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class ChallengeIn(BaseModel):
    challenge: str = Field(max_length=200)


class MfaIn(ChallengeIn):
    code: str = Field(max_length=10)


class PasswordIn(BaseModel):
    old_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    _limiter(request, "login").check(client_ip(request) or "?")
    try:
        result = auth_service.login(db, ctx, body.email, body.password, client_ip(request), request.headers.get("user-agent"))
    except auth_service.AuthError as exc:
        db.commit()  # keep failed-attempt counters and audit entries
        raise api_error(exc.status, exc.code, str(exc)) from exc
    db.commit()
    return {"status": result.status, "challenge": result.challenge}


@router.post("/auth/mfa/enroll")
def mfa_enroll(body: ChallengeIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    try:
        data = auth_service.begin_mfa_enrollment(db, ctx, body.challenge)
    except auth_service.AuthError as exc:
        raise api_error(exc.status, exc.code, str(exc)) from exc
    db.commit()
    return data


@router.post("/auth/mfa/verify")
def mfa_verify(body: MfaIn, request: Request, response: Response, ctx: AppContext = Depends(get_ctx),
               db: Session = Depends(get_db)):
    _limiter(request, "mfa").check(client_ip(request) or "?")
    try:
        token = auth_service.complete_mfa(db, ctx, body.challenge, body.code, client_ip(request),
                                          request.headers.get("user-agent"))
    except auth_service.AuthError as exc:
        db.commit()
        raise api_error(exc.status, exc.code, str(exc)) from exc
    db.commit()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=ctx.settings.cookie_secure, samesite="strict",
                        max_age=ctx.settings.session_absolute_hours * 3600, path="/")
    return {"token": token, "token_type": "bearer"}


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db),
           principal: Principal = Depends(current_principal)):
    token = request.headers.get("authorization", "")[7:] or request.cookies.get(SESSION_COOKIE)
    auth_service.logout(db, token)
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/password")
def change_password(body: PasswordIn, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db),
                    principal: Principal = Depends(current_principal)):
    try:
        auth_service.change_password(db, ctx, principal.user, body.old_password, body.new_password)
    except auth_service.AuthError as exc:
        raise api_error(exc.status, exc.code, str(exc)) from exc
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db), principal: Principal = Depends(current_principal)):
    pending = agreement_service.pending_documents(db, principal.user, principal.firm, principal.lang)
    return {
        "id": principal.user.id,
        "name": principal.user.name,
        "email": auth_service.user_email(ctx, principal.user),
        "role": principal.user.role,
        "firm_id": principal.user.firm_id,
        "language": principal.user.language,
        "permissions": sorted(p.value for p in ROLE_PERMISSIONS[principal.role]),
        "pending_agreements": [d.id for d in pending],
    }


def _doc_view(d: AgreementDocument) -> dict:
    return {"document_id": d.id, "kind": d.kind, "version": d.version, "language": d.language, "title": d.title,
            "body": d.body, "content_hash": d.content_hash, "material": d.material,
            "counsel_reviewed": d.counsel_reviewed}


@router.get("/agreements/pending")
def agreements_pending(db: Session = Depends(get_db), principal: Principal = Depends(current_principal)):
    from ...compliance.clickwrap import CHECKBOX_TEXT

    idx = 0 if principal.lang == "id" else 1
    return [
        {**_doc_view(d), "checkbox_text": CHECKBOX_TEXT[AgreementKind(d.kind)][idx], "checked": False}
        for d in agreement_service.pending_documents(db, principal.user, principal.firm, principal.lang)
    ]


class AcceptItem(BaseModel):
    document_id: int
    accepted: bool  # must be explicitly true; there is no default
    content_hash: str = Field(min_length=64, max_length=64)


class AcceptIn(BaseModel):
    items: list[AcceptItem]


@router.post("/agreements/accept")
def agreements_accept(body: AcceptIn, request: Request, db: Session = Depends(get_db),
                      principal: Principal = Depends(current_principal)):
    try:
        rows = agreement_service.accept(
            db, principal.user, principal.firm,
            [agreement_service.AcceptanceItem(i.document_id, i.accepted, i.content_hash) for i in body.items],
            ip=principal.ip, user_agent=request.headers.get("user-agent"),
        )
    except agreement_service.AgreementError as exc:
        raise api_error(422, exc.code, str(exc), details=exc.details) from exc
    db.commit()
    return {"accepted": [{"kind": r.kind, "version": r.version, "accepted_at_wib": r.accepted_at_wib} for r in rows]}


@router.get("/agreements/history")
def agreements_history(db: Session = Depends(get_db), principal: Principal = Depends(current_principal)):
    return agreement_service.acceptance_history(db, principal.user.id)


@router.get("/agreements/current/{kind}")
def agreement_current(kind: AgreementKind, lang: str = "id", db: Session = Depends(get_db)):
    doc = agreement_service.current_document(db, kind, "en" if lang == "en" else "id")
    if doc is None:
        raise api_error(404, "NOT_FOUND", "Dokumen belum diterbitkan")
    return _doc_view(doc)
