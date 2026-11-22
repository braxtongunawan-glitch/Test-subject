"""Unauthenticated endpoints reached from e-mails: opt-out, preference centre, privacy notice, ESP webhook."""

from __future__ import annotations

import hashlib
import hmac
import html
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...compliance import tokens
from ...db.models import Contact
from ...domain.enums import AgreementKind, Purpose
from ...domain.i18n import label
from ...services import agreements as agreement_service
from ...services import communications as comms
from ...services import contacts as contact_service
from ...services.audit import Actor
from ...services.context import AppContext
from ..deps import api_error, client_ip, get_ctx, get_db

router = APIRouter(tags=["public"])

_TEXT = {
    "id": {
        "title": "Berhenti berlangganan",
        "confirm": "Klik tombol di bawah untuk berhenti menerima email jenis ini:",
        "all": "Berhenti dari semua email dari pengirim ini",
        "button": "Berhenti berlangganan",
        "done": "Permintaan Anda telah kami proses. Anda tidak akan menerima email jenis ini lagi.",
        "done_all": "Permintaan Anda telah kami proses. Anda tidak akan menerima email dari kami lagi.",
        "invalid": "Tautan tidak valid atau sudah tidak berlaku.",
        "prefs": "Preferensi email",
        "prefs_hint": "Pilih jenis email yang ingin Anda terima.",
        "save": "Simpan",
        "saved": "Preferensi Anda telah disimpan.",
    },
    "en": {
        "title": "Unsubscribe",
        "confirm": "Click the button below to stop receiving this type of e-mail:",
        "all": "Stop all e-mails from this sender",
        "button": "Unsubscribe",
        "done": "Your request has been processed. You will not receive this type of e-mail again.",
        "done_all": "Your request has been processed. You will not receive e-mails from us again.",
        "invalid": "This link is invalid or no longer valid.",
        "prefs": "E-mail preferences",
        "prefs_hint": "Choose which e-mails you want to receive.",
        "save": "Save",
        "saved": "Your preferences have been saved.",
    },
}


def _page(lang: str, title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>body{{font-family:system-ui,Arial,sans-serif;max-width:34rem;"
        "margin:3rem auto;padding:0 1rem;line-height:1.5;color:#1f2933}button{padding:.6rem 1.2rem;font-size:1rem;"
        "cursor:pointer}label{display:block;margin:.4rem 0}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(doc, status_code=status)


def _resolve(db: Session, ctx: AppContext, token: str, kind: str | None = None) -> tuple[Contact, dict] | None:
    try:
        data = tokens.verify(token, ctx.token_key)
    except tokens.InvalidToken:
        return None
    if kind and data.get("k") != kind:
        return None
    contact = db.get(Contact, data.get("c"))
    if contact is None or contact.firm_id != data.get("f"):
        return None
    return contact, data


@router.get("/berhenti-langganan/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    found = _resolve(db, ctx, token)
    if not found:
        return _page("id", _TEXT["id"]["title"], f"<p>{_TEXT['id']['invalid']}</p>", 404)
    contact, data = found
    t = _TEXT[contact.language]
    purpose = label(Purpose(data["p"]), contact.language) if data.get("p") in Purpose._value2member_map_ else ""
    body = (
        f"<p>{t['confirm']} <strong>{html.escape(purpose)}</strong></p>"
        f'<form method="post"><label><input type="checkbox" name="all" value="1"> {t["all"]}</label>'
        f'<button type="submit">{t["button"]}</button></form>'
    )
    return _page(contact.language, t["title"], body)


@router.post("/berhenti-langganan/{token}", response_class=HTMLResponse)
async def unsubscribe(token: str, request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    """Handles both the confirmation form and RFC 8058 one-click POSTs from mail clients."""
    found = _resolve(db, ctx, token)
    if not found:
        return _page("id", _TEXT["id"]["title"], f"<p>{_TEXT['id']['invalid']}</p>", 404)
    contact, data = found
    form = (await request.body()).decode(errors="ignore")
    opt_all = "all=1" in form.split("&")
    purpose = None if opt_all or data.get("p") not in Purpose._value2member_map_ else Purpose(data["p"])
    actor = Actor(None, "data-subject", contact.firm_id, client_ip(request))
    contact_service.opt_out(db, ctx, contact, purpose, "OPT_OUT_LINK", actor)
    db.commit()
    t = _TEXT[contact.language]
    return _page(contact.language, t["title"], f"<p>{t['done_all'] if purpose is None else t['done']}</p>")


@router.get("/preferensi/{token}", response_class=HTMLResponse)
def preferences_page(token: str, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    found = _resolve(db, ctx, token, "prefs")
    if not found:
        return _page("id", "Preferensi", f"<p>{_TEXT['id']['invalid']}</p>", 404)
    contact, _ = found
    lang = contact.language
    t = _TEXT[lang]
    opted = set(contact.opted_out_purposes or [])
    boxes = "".join(
        f'<label><input type="checkbox" name="p" value="{p.value}"{"" if contact.opted_out_all or p.value in opted else " checked"}>'
        f" {html.escape(label(p, lang))}</label>"
        for p in Purpose
    )
    body = f'<p>{t["prefs_hint"]}</p><form method="post">{boxes}<button type="submit">{t["save"]}</button></form>'
    return _page(lang, t["prefs"], body)


@router.post("/preferensi/{token}", response_class=HTMLResponse)
async def save_preferences(token: str, request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    found = _resolve(db, ctx, token, "prefs")
    if not found:
        return _page("id", "Preferensi", f"<p>{_TEXT['id']['invalid']}</p>", 404)
    contact, _ = found
    form = (await request.body()).decode(errors="ignore")
    wanted = {pair.split("=", 1)[1] for pair in form.split("&") if pair.startswith("p=")}
    actor = Actor(None, "data-subject", contact.firm_id, client_ip(request))
    current_opted = set(contact.opted_out_purposes or [])
    for p in Purpose:
        want = p.value in wanted
        was_receiving = not contact.opted_out_all and p.value not in current_opted
        if want != was_receiving:
            contact_service.record_consent(db, contact, p, want, "PREFERENCE_CENTER", actor)
    # A firm-wide opt-out (and its suppression entry) is deliberately not lifted from this page: a forwarded
    # e-mail could otherwise re-subscribe someone. Lifting it requires contacting the firm.
    db.commit()
    t = _TEXT[contact.language]
    return _page(contact.language, t["prefs"], f"<p>{t['saved']}</p>")


@router.get("/kebijakan-privasi", response_class=HTMLResponse)
def privacy_notice(lang: str = "id", db: Session = Depends(get_db)):
    lang = "en" if lang == "en" else "id"
    doc = agreement_service.current_document(db, AgreementKind.PRIVACY_POLICY, lang)
    if doc is None:
        return _page(lang, "Kebijakan Privasi", "<p>-</p>", 404)
    body = "".join(f"<p>{html.escape(par)}</p>" for par in doc.body.split("\n") if par.strip() and not par.startswith("# "))
    return _page(lang, doc.title, f"<p><small>Versi {html.escape(doc.version)}</small></p>{body}")


@router.post("/webhooks/esp")
async def esp_webhook(request: Request, ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)):
    raw = await request.body()
    secret = ctx.settings.esp_webhook_secret
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("x-idpatent-signature", "")):
            raise api_error(401, "BAD_SIGNATURE", "Tanda tangan webhook tidak valid")
    elif ctx.settings.production:
        raise api_error(503, "WEBHOOK_NOT_CONFIGURED", "Webhook belum dikonfigurasi")
    try:
        payload = json.loads(raw or b"{}")
        events = payload if isinstance(payload, list) else [payload]
        handled = 0
        for ev in events[:500]:
            if comms.handle_esp_event(db, ctx, ev):
                handled += 1
    except (ValueError, comms.CommunicationError) as exc:
        raise api_error(422, "INVALID_EVENT", str(exc)) from exc
    db.commit()
    return {"handled": handled}
