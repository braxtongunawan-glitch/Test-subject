import hashlib
import hmac
import io
import json
from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import text

from conftest import PASSWORD

PATENT = {
    "application_number": "P00201502345",
    "registration_number": "IDP000045678",
    "title": "Alat penyaring air",
    "filing_date": "2015-03-10",
    "grant_date": "2019-08-20",
    "owner_name": "PT. Air Bersih Tbk",
    "paid_through_year": 12,
}

CSV = """nomor_permohonan;nomor_paten;jenis;judul;tanggal_penerimaan;tanggal_pemberian;pemegang;status;dibayar_sampai_tahun
P00201600111;IDP000050001;Paten;Metode fermentasi;12/05/2016;3 Januari 2020;Institut Teknologi Bandung;Diberi;8
S00202000222;IDS000007777;Paten Sederhana;Alat tanam;15/01/2020;01/06/2021;ITB;Diberi;
P00202400333;;Paten;Baterai;01/05/2024;;PT Energi Nusantara;Dalam Proses;
XYZ;;Paten;Rusak;01/01/2020;;PT Rusak;;
P00201800444;;Paten;Tanpa tanggal;;;PT Kurang Data;;
"""


def create_patent(env, headers, **overrides):
    body = {**PATENT, **overrides}
    r = env.client.post("/patents", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["patent"]


# ------------------------------------------------------------------ auth & agreements
def test_login_mfa_and_agreement_gate(env):
    c = env.client
    assert c.get("/patents").status_code == 401
    r = c.post("/auth/login", json={"email": "admin@contoh.co.id", "password": "salah"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "INVALID_CREDENTIALS"
    headers = env.login("admin@contoh.co.id", accept=False)
    r = c.get("/patents", headers=headers)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "AGREEMENT_ACCEPTANCE_REQUIRED"
    pending = c.get("/agreements/pending", headers=headers).json()
    assert {d["kind"] for d in pending} == {"TERMS_OF_SERVICE", "PRIVACY_POLICY", "DATA_PROCESSING_NOTICE"}
    assert all(d["checked"] is False for d in pending)  # never pre-ticked
    # accepting only some documents is rejected
    r = c.post("/agreements/accept", headers=headers, json={"items": [
        {"document_id": pending[0]["document_id"], "accepted": True, "content_hash": pending[0]["content_hash"]}]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "ACCEPTANCE_REQUIRED"
    # a stale hash (text changed since display) is rejected
    r = c.post("/agreements/accept", headers=headers, json={"items": [
        {"document_id": d["document_id"], "accepted": True, "content_hash": "0" * 64} for d in pending]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "STALE_DOCUMENT"
    r = c.post("/agreements/accept", headers=headers, json={"items": [
        {"document_id": d["document_id"], "accepted": True, "content_hash": d["content_hash"]} for d in pending]})
    assert r.status_code == 200 and len(r.json()["accepted"]) == 3
    assert c.get("/patents", headers=headers).status_code == 200
    history = c.get("/agreements/history", headers=headers).json()
    assert history[0]["accepted_at_wib"].endswith("WIB") and history[0]["checkbox_text"].startswith("Saya")


def test_lockout_after_repeated_failures(env):
    for _ in range(5):
        env.client.post("/auth/login", json={"email": "viewer@contoh.co.id", "password": "salah"})
    r = env.client.post("/auth/login", json={"email": "viewer@contoh.co.id", "password": PASSWORD})
    assert r.status_code == 423 and r.json()["detail"]["error"] == "ACCOUNT_LOCKED"


def test_cookie_requests_need_csrf_header(env):
    headers = env.login("admin@contoh.co.id")
    token = headers["Authorization"].split()[1]
    env.client.cookies.set("idp_session", token)
    r = env.client.post("/patents", json=PATENT)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "CSRF_HEADER_REQUIRED"
    r = env.client.post("/patents", json=PATENT, headers={"X-Requested-With": "idpatent"})
    assert r.status_code == 201
    env.client.cookies.clear()


def test_material_policy_update_forces_reacceptance(env):
    from idpatent.domain.enums import AgreementKind
    from idpatent.services import agreements
    from idpatent.services.audit import Actor

    headers = env.login("am@contoh.co.id")
    with env.ctx.sessions() as db:
        agreements.publish_version(db, AgreementKind.PRIVACY_POLICY, "0.2-draft",
                                   {"id": "# Kebijakan Privasi\n\nVersi baru.", "en": "# Privacy Policy\n\nNew."},
                                   material=True, effective_at=agreements.utcnow(), counsel_reviewed=False,
                                   actor=Actor.system(), production=False)
        db.commit()
    r = env.client.get("/patents", headers=headers)
    assert r.status_code == 403
    assert [p["kind"] for p in r.json()["detail"]["pending"]] == ["PRIVACY_POLICY"]


# ------------------------------------------------------------------ patents
def test_create_patent_and_detail(env):
    headers = env.login("am@contoh.co.id")
    p = create_patent(env, headers)
    assert p["status"] == "GRANTED" and p["status_label"] == "Diberi"
    assert p["protection_end"]["text"] == "10 Maret 2035"
    assert p["next_annuity_due"]["iso"] == "2027-02-10"
    assert p["next_working_statement_due"]["iso"] == "2024-12-31"  # nothing filed yet: 2024 is overdue
    assert p["owner"]["type"] == "COMPANY"
    detail = env.client.get(f"/patents/{p['id']}?lang=en", headers=headers).json()
    assert detail["status_label"] == "Granted / Active"
    assert detail["annuities"][0]["years_text"] == "years 1-6"
    assert any(w["code"] == "RULES_NOT_COUNSEL_VERIFIED" for w in detail["warnings"])
    assert detail["provenance"]["source"] == "USER_INPUT" and detail["provenance"]["verification_status"] == "UNVERIFIED"
    # update via the registration number only: same patent, no duplicate
    r = env.client.post("/patents", headers=headers, json={"registration_number": "IDP 000 045 678", "title": "Judul baru"})
    assert r.json()["created"] is False and "title" in r.json()["changed_fields"]


def test_account_manager_cannot_mark_official_source_without_verify(env):
    headers = env.login("viewer@contoh.co.id")
    assert env.client.post("/patents", headers=headers, json=PATENT).status_code == 403


def test_lookup_normalizes_and_links_pdki(env):
    headers = env.login("am@contoh.co.id")
    create_patent(env, headers)
    r = env.client.get("/lookup", params={"number": "idp-000-045678"}, headers=headers).json()
    assert r["normalized"] == "IDP000045678" and r["patent"]["application_number"] == "P00201502345"
    assert r["pdki_search_url"].startswith("https://pdki-indonesia.dgip.go.id/")
    assert env.client.get("/lookup", params={"number": "bukan nomor"}, headers=headers).status_code == 422


def test_csv_import_with_owner_matching_and_errors(env):
    headers = env.login("am@contoh.co.id")
    r = env.client.post("/patents/import", headers=headers,
                        files={"file": ("portofolio.csv", CSV.encode(), "text/csv")}, data={"source": "USER_INPUT"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["created"] == 3 and s["rows"] == 5
    assert {e["row"] for e in s["errors"]} == {5, 6}
    owners = env.client.get("/owners", headers=headers).json()
    itb = [o for o in owners if "BANDUNG" in o["normalized_key"]]
    assert len(itb) == 1 and itb[0]["owner_type"] == "UNIVERSITY"  # "ITB" resolved to the same owner
    pending = env.client.get("/patents", params={"status": "PENDING"}, headers=headers).json()
    assert [p["application_number"] for p in pending] == ["P00202400333"]


def test_xlsx_import(env):
    headers = env.login("admin@contoh.co.id")
    wb = Workbook()
    ws = wb.active
    ws.append(["Application Number", "Patent Number", "Type", "Filing Date", "Grant Date", "Owner", "Country"])
    ws.append(["P00201700555", "IDP000060001", "Patent", date(2017, 2, 1), date(2021, 9, 30), "Samsung Electronics Co., Ltd.", "KR"])
    buf = io.BytesIO()
    wb.save(buf)
    r = env.client.post("/patents/import", headers=headers,
                        files={"file": ("export.xlsx", buf.getvalue(), "application/octet-stream")},
                        data={"source": "AGENT_FEED"})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    p = env.client.get("/patents", headers=headers).json()[0]
    assert p["owner"]["type"] == "FOREIGN" and p["source"] == "AGENT_FEED" and p["verification_status"] == "VERIFIED"


def test_lower_priority_source_does_not_overwrite_verified_data(env):
    headers = env.login("admin@contoh.co.id")
    create_patent(env, headers, source="MANUAL_VERIFIED", verified_on="2026-11-01")
    r = env.client.post("/patents", headers=headers, json={**PATENT, "grant_date": "2019-09-01", "source": "USER_INPUT"})
    body = r.json()
    assert any(w["code"] == "LOWER_PRIORITY_SOURCE_FIELDS_SKIPPED" for w in body["warnings"])
    assert body["patent"]["grant_date"]["iso"] == "2019-08-20"


def test_override_payment_and_working_statement(env):
    headers = env.login("am@contoh.co.id")
    p = create_patent(env, headers, paid_through_year=7)
    assert p["status"] == "LAPSE_SUSPECTED"
    r = env.client.post(f"/patents/{p['id']}/override", headers=headers,
                        json={"field": "paid_through_year", "value": 12, "reason": "Bukti bayar SIMPAKI diperiksa"})
    assert r.status_code == 200 and r.json()["status"] == "GRANTED"
    assert r.json()["provenance"]["verification_status"] == "OVERRIDDEN"
    r = env.client.post(f"/patents/{p['id']}/payments", headers=headers,
                        json={"first_year": 13, "last_year": 13, "paid_on": "2026-11-20", "amount_idr": 1500000,
                              "reference": "SIMPAKI-820001"})
    assert r.json()["next_annuity_due"]["iso"] == "2028-02-10"
    r = env.client.post(f"/patents/{p['id']}/working-statements", headers=headers,
                        json={"period": "2026", "submitted_on": "2026-11-21", "declaration": "NOT_WORKED"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "REASON_REQUIRED"
    r = env.client.post(f"/patents/{p['id']}/working-statements", headers=headers,
                        json={"period": "2026", "submitted_on": "2026-11-21", "declaration": "IMPORTED"})
    assert r.status_code == 201
    ws = {w["period"]: w["status"] for w in r.json()["working_statements"]}
    assert ws["2026"] == "SUBMITTED" and ws["2024"] == "OVERDUE"


# ------------------------------------------------------------------ alerts & reports
def test_alert_run_is_idempotent_and_stages_advance(env):
    headers = env.login("am@contoh.co.id")
    p = create_patent(env, headers)
    first = {a["kind"]: a for a in env.client.get("/alerts", headers=headers).json()}
    assert first["ANNUITY_DUE"]["stage"] == "90d"
    assert "Biaya tahunan paten IDP000045678 jatuh tempo 10 Februari 2027" == first["ANNUITY_DUE"]["title"]
    assert env.client.post("/alerts/run", headers=headers, json={}).json()["new_alerts"] == 0
    env.set_today(date(2026, 12, 15))
    run = env.client.post("/alerts/run", headers=headers, json={}).json()
    assert run["new_alerts"] >= 1
    open_alerts = env.client.get("/alerts", headers=headers).json()
    annuity = [a for a in open_alerts if a["kind"] == "ANNUITY_DUE"]
    assert [a["stage"] for a in annuity] == ["60d"]  # the 90-day stage was superseded
    r = env.client.post(f"/alerts/{annuity[0]['id']}/acknowledge", headers=headers)
    assert r.json()["status"] == "ACKNOWLEDGED"
    assert p["id"] == annuity[0]["patent_id"]


def test_status_change_alert_on_import(env):
    headers = env.login("admin@contoh.co.id")
    create_patent(env, headers)
    env.client.post("/patents", headers=headers, json={"registration_number": "IDP000045678", "recorded_status": "LAPSED"})
    kinds = [a["kind"] for a in env.client.get("/alerts", headers=headers).json()]
    assert "STATUS_CHANGE" in kinds


def test_dashboard_and_csv_export(env):
    headers = env.login("admin@contoh.co.id")
    env.client.post("/patents/import", headers=headers, files={"file": ("p.csv", CSV.encode(), "text/csv")})
    d = env.client.get("/reports/dashboard", headers=headers).json()
    assert d["totals"]["patents"] == 3
    assert {x["code"] for x in d["by_owner_type"]} >= {"UNIVERSITY", "COMPANY"}
    r = env.client.get("/reports/portfolio.csv", headers=headers)
    assert r.status_code == 200 and "Nomor Permohonan;Nomor Paten" in r.text and "Perguruan Tinggi" in r.text
    r = env.client.get("/reports/portfolio.csv?lang=en", headers=headers)
    assert "Application No.,Patent No." in r.text


# ------------------------------------------------------------------ RBAC & tenancy
def test_viewer_sees_masked_contacts_and_tenant_isolation(env):
    admin = env.login("admin@contoh.co.id")
    p = create_patent(env, admin)
    env.client.post("/contacts", headers=admin, json={"email": "sari.dewi@airbersih.co.id", "name": "Sari Dewi",
                                                      "owner_id": p["owner"]["id"], "lawful_basis": "CONTRACT"})
    viewer = env.login("viewer@contoh.co.id")
    contacts = env.client.get("/contacts", headers=viewer).json()
    assert contacts[0]["email"] == "s***i@airbersih.co.id" and contacts[0]["phone"] is None
    other = env.login("admin@lain.co.id")
    assert env.client.get(f"/patents/{p['id']}", headers=other).status_code == 404
    assert env.client.get("/patents", headers=other).json() == []


def test_client_portal_scope(env):
    admin = env.login("admin@contoh.co.id")
    env.client.post("/patents/import", headers=admin, files={"file": ("p.csv", CSV.encode(), "text/csv")})
    owners = {o["normalized_key"]: o["id"] for o in env.client.get("/owners", headers=admin).json()}
    itb = owners["INSTITUT TEKNOLOGI BANDUNG"]
    r = env.client.post("/admin/users", headers=admin, json={"email": "hki@itb.ac.id", "name": "TTO ITB",
                                                             "role": "CLIENT_USER", "password": PASSWORD,
                                                             "owner_scope": [itb]})
    assert r.status_code == 201, r.text
    client = env.login("hki@itb.ac.id")
    patents = env.client.get("/portal/patents", headers=client).json()
    assert len(patents) == 2 and all(p["owner"]["id"] == itb for p in patents)
    assert env.client.get("/patents", headers=client).status_code == 403
    others = [p for p in env.client.get("/patents", headers=admin).json() if p["owner"]["id"] != itb]
    assert env.client.get(f"/portal/patents/{others[0]['id']}", headers=client).status_code == 404


# ------------------------------------------------------------------ correspondence
def _client_contact(env, headers, owner_id, email="sari.dewi@airbersih.co.id"):
    r = env.client.post("/contacts", headers=headers, json={"email": email, "name": "Sari Dewi", "owner_id": owner_id,
                                                            "relationship": "CLIENT", "lawful_basis": "CONTRACT"})
    assert r.status_code == 201, r.text
    return r.json()


def test_client_annuity_reminder_end_to_end(env):
    am = env.login("am@contoh.co.id")
    p = create_patent(env, am)
    contact = _client_contact(env, am, p["owner"]["id"])
    r = env.client.post("/campaigns", headers=am, json={"name": "Pengingat Februari", "purpose": "ANNUITY_REMINDER",
                                                        "auto_within_days": 120})
    assert r.status_code == 201, r.text
    camp = r.json()
    assert camp["recipients"] == 1 and camp["requires_approval"] is False
    assert env.client.post(f"/campaigns/{camp['id']}/submit", headers=am).json()["status"] == "APPROVED"
    detail = env.client.get(f"/campaigns/{camp['id']}", headers=am).json()
    preview = env.client.get(f"/campaigns/{camp['id']}/preview/{detail['communications'][0]['id']}", headers=am).json()
    assert "tahun ke-13" in preview["text"]
    sent = env.client.post(f"/campaigns/{camp['id']}/send", headers=am).json()
    assert sent["result"] == {"sent": 1, "blocked": 0, "status": "SENT"}
    comm = sent["communications"][0]
    assert comm["lawful_basis"] == "CONTRACT" and "TEMPLATE_NOT_COUNSEL_APPROVED" in comm["warnings"]
    msg = env.outbox.sent[0]
    assert msg.to == "sari.dewi@airbersih.co.id"
    assert msg.subject.startswith("Pengingat biaya tahunan paten IDP000045678")
    assert "Jl. Jend. Sudirman Kav. 1" in msg.text and "Pelaksanaan perjanjian" in msg.text
    assert msg.headers()["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert contact["id"] == comm["contact_id"]
    # ESP events: open, then reply tracked on the thread
    body = json.dumps([{"event": "opened", "message_id": msg.message_id},
                       {"event": "replied", "message_id": msg.message_id}]).encode()
    sig = "sha256=" + hmac.new(b"whsec-test", body, hashlib.sha256).hexdigest()
    r = env.client.post("/webhooks/esp", content=body, headers={"X-IDPatent-Signature": sig})
    assert r.json() == {"handled": 2}
    comm = env.client.get("/communications", headers=am).json()[0]
    assert comm["status"] == "REPLIED" and comm["opened_at"] and comm["replied_at"]
    assert env.client.post("/webhooks/esp", content=body, headers={"X-IDPatent-Signature": "sha256=00"}).status_code == 401


def test_commercial_offer_to_client_requires_consent(env):
    am = env.login("am@contoh.co.id")
    p = create_patent(env, am)
    contact = _client_contact(env, am, p["owner"]["id"])
    camp = env.client.post("/campaigns", headers=am, json={"name": "Penawaran", "purpose": "COMMERCIAL_OFFER",
                                                           "recipients": [{"contact_id": contact["id"], "patent_ids": [p["id"]]}]}).json()
    env.client.post(f"/campaigns/{camp['id']}/submit", headers=am)
    res = env.client.post(f"/campaigns/{camp['id']}/send", headers=am).json()
    assert res["communications"][0]["blocked_reasons"] == ["COMMERCIAL_OFFER_NEEDS_CONSENT_OR_LIA"]
    assert env.outbox.sent == []


def test_patent_must_belong_to_contact_owner(env):
    am = env.login("am@contoh.co.id")
    p = create_patent(env, am)
    q = create_patent(env, am, application_number="P00201600999", registration_number="IDP000099999",
                      owner_name="PT Lain Sekali")
    contact = _client_contact(env, am, p["owner"]["id"])
    r = env.client.post("/campaigns", headers=am, json={"name": "x", "purpose": "ANNUITY_REMINDER",
                                                        "recipients": [{"contact_id": contact["id"], "patent_ids": [q["id"]]}]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "PATENT_NOT_OWNED_BY_CONTACT_OWNER"


def test_cold_outreach_requires_lia_approval_and_review(env):
    admin = env.login("admin@contoh.co.id")
    compliance = env.login("compliance@contoh.co.id")
    am = env.login("am@contoh.co.id")
    p = create_patent(env, am)
    owner_id = p["owner"]["id"]
    lia_body = {"title": "Pemberitahuan biaya tahunan kepada pemegang paten",
                "purpose": "Memberitahu pemegang paten tentang tenggat biaya tahunan paten tertentu.",
                "necessity": "Tanpa pemberitahuan, paten dapat dihapus karena kelalaian pembayaran.",
                "balancing": "Pesan rendah volume, relevan, alamat institusional, opt-out satu klik.",
                "safeguards": "Batas harian, persetujuan peninjau, daftar supresi global, tanpa email pribadi."}
    assert env.client.post("/compliance/lias", headers=am, json=lia_body).status_code == 403
    lia = env.client.post("/compliance/lias", headers=admin, json=lia_body).json()
    r = env.client.post("/contacts", headers=am, json={"email": "legal@airbersih.co.id", "owner_id": owner_id,
                                                       "relationship": "PROSPECT", "lawful_basis": "LEGITIMATE_INTEREST"})
    assert r.json()["detail"]["error"] == "LIA_REQUIRED"
    base = {"owner_id": owner_id, "relationship": "PROSPECT", "lawful_basis": "LEGITIMATE_INTEREST", "lia_id": lia["id"],
            "basis_note": "Email institusi dari situs resmi perusahaan, diambil 20 Nov 2026."}
    institutional = env.client.post("/contacts", headers=am, json={**base, "email": "legal@airbersih.co.id"}).json()
    personal = env.client.post("/contacts", headers=am, json={**base, "email": "pemilik.airbersih@gmail.com"}).json()
    assert personal["is_personal_email"] is True
    recipients = [{"contact_id": institutional["id"], "patent_ids": [p["id"]]},
                  {"contact_id": personal["id"], "patent_ids": [p["id"]]}]
    camp = env.client.post("/campaigns", headers=am, json={"name": "Prospek", "purpose": "ANNUITY_REMINDER",
                                                           "recipients": recipients}).json()
    assert camp["is_cold"] and camp["requires_approval"]
    assert env.client.post(f"/campaigns/{camp['id']}/submit", headers=am).json()["status"] == "PENDING_APPROVAL"
    assert env.client.post(f"/campaigns/{camp['id']}/send", headers=am).status_code == 422
    assert env.client.post(f"/campaigns/{camp['id']}/review", headers=am, json={"approve": True}).status_code == 403
    r = env.client.post(f"/campaigns/{camp['id']}/review", headers=admin, json={"approve": True})
    assert r.json()["detail"]["error"] == "COLD_REVIEW_NOTE_REQUIRED"
    r = env.client.post(f"/campaigns/{camp['id']}/review", headers=admin,
                        json={"approve": True, "note": "Ditinjau: relevan, alamat institusional, LIA #1."})
    assert r.json()["status"] == "APPROVED"
    # LIA not yet approved -> everything blocked
    res = env.client.post(f"/campaigns/{camp['id']}/send", headers=am).json()
    assert res["result"]["blocked"] == 2
    assert all("LIA_NOT_APPROVED" in c["blocked_reasons"] for c in res["communications"])
    # four-eyes on the LIA, then a fresh campaign
    assert env.client.post(f"/compliance/lias/{lia['id']}/approve", headers=admin,
                           json={"review_due": "2027-11-22"}).json()["detail"]["error"] == "FOUR_EYES"
    assert env.client.post(f"/compliance/lias/{lia['id']}/approve", headers=compliance,
                           json={"review_due": "2027-11-22"}).json()["approved"]
    camp2 = env.client.post("/campaigns", headers=am, json={"name": "Prospek 2", "purpose": "ANNUITY_REMINDER",
                                                            "recipients": recipients}).json()
    env.client.post(f"/campaigns/{camp2['id']}/submit", headers=am)
    env.client.post(f"/campaigns/{camp2['id']}/review", headers=compliance,
                    json={"approve": True, "note": "Ditinjau ulang setelah LIA disetujui."})
    res = env.client.post(f"/campaigns/{camp2['id']}/send", headers=am).json()
    by_contact = {c["contact_id"]: c for c in res["communications"]}
    assert by_contact[institutional["id"]]["status"] == "SENT"
    assert "COLD_OUTREACH_GREY_AREA_UU_PDP" in by_contact[institutional["id"]]["warnings"]
    assert by_contact[personal["id"]]["blocked_reasons"] == ["COLD_OUTREACH_TO_PERSONAL_EMAIL"]
    assert "basis data publik PDKI" in env.outbox.sent[-1].text


def test_one_click_opt_out_and_preferences(env):
    am = env.login("am@contoh.co.id")
    p = create_patent(env, am)
    contact = _client_contact(env, am, p["owner"]["id"])

    def send():
        camp = env.client.post("/campaigns", headers=am, json={"name": "x", "purpose": "ANNUITY_REMINDER",
                                                               "recipients": [{"contact_id": contact["id"], "patent_ids": [p["id"]]}]}).json()
        env.client.post(f"/campaigns/{camp['id']}/submit", headers=am)
        return env.client.post(f"/campaigns/{camp['id']}/send", headers=am).json()

    assert send()["result"]["sent"] == 1
    msg = env.outbox.sent[-1]
    path = msg.unsubscribe_url.replace("https://app.test", "")
    page = env.client.get(path)
    assert page.status_code == 200 and "Pengingat biaya tahunan paten" in page.text
    # RFC 8058 one-click POST from the mail client
    r = env.client.post(path, content=b"List-Unsubscribe=One-Click",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and "tidak akan menerima email jenis ini" in r.text
    res = send()
    assert res["result"]["blocked"] == 1 and "OPTED_OUT" in res["communications"][0]["blocked_reasons"]
    # preference centre shows the purpose as unticked; tampered tokens are rejected
    prefs_path = msg.text.split("Atur preferensi email: ")[1].split()[0].replace("https://app.test", "")
    page = env.client.get(prefs_path).text
    assert 'value="ANNUITY_REMINDER">' in page and 'value="EXPIRY_NOTICE" checked' in page
    assert env.client.get(path[:-3] + "abc").status_code == 404
    detail = env.client.get(f"/contacts/{contact['id']}", headers=am).json()
    assert detail["consents"]["ANNUITY_REMINDER"] is False


def test_hard_bounce_suppresses_address(env):
    am = env.login("am@contoh.co.id")
    p = create_patent(env, am)
    contact = _client_contact(env, am, p["owner"]["id"])
    camp = env.client.post("/campaigns", headers=am, json={"name": "x", "purpose": "ANNUITY_REMINDER",
                                                           "recipients": [{"contact_id": contact["id"], "patent_ids": [p["id"]]}]}).json()
    env.client.post(f"/campaigns/{camp['id']}/submit", headers=am)
    env.client.post(f"/campaigns/{camp['id']}/send", headers=am)
    body = json.dumps({"event": "bounced", "bounce_type": "hard", "message_id": env.outbox.sent[0].message_id}).encode()
    sig = "sha256=" + hmac.new(b"whsec-test", body, hashlib.sha256).hexdigest()
    env.client.post("/webhooks/esp", content=body, headers={"X-IDPatent-Signature": sig})
    admin = env.login("admin@contoh.co.id")
    reasons = [s["reason"] for s in env.client.get("/compliance/suppressions", headers=admin).json()]
    assert reasons == ["HARD_BOUNCE"]


def test_dsar_export_and_erasure(env):
    admin = env.login("admin@contoh.co.id")
    p = create_patent(env, admin)
    contact = _client_contact(env, admin, p["owner"]["id"])
    env.client.post(f"/contacts/{contact['id']}/consent", headers=admin,
                    json={"purpose": "COMMERCIAL_OFFER", "granted": True, "evidence": "Formulir acara 12 Nov 2026"})
    export = env.client.get(f"/contacts/{contact['id']}/dsar-export", headers=admin).json()
    assert export["contact"]["email"] == "sari.dewi@airbersih.co.id"
    assert export["consent_history"][0]["purpose"] == "COMMERCIAL_OFFER"
    assert env.client.post(f"/contacts/{contact['id']}/erase", headers=admin).json() == {"ok": True}
    erased = env.client.get(f"/contacts/{contact['id']}", headers=admin).json()
    assert erased["erased"] and erased["email"] is None and erased["name"] is None
    # the same address cannot be re-added and contacted silently: it is suppressed
    again = _client_contact(env, admin, p["owner"]["id"])
    camp = env.client.post("/campaigns", headers=admin, json={"name": "x", "purpose": "ANNUITY_REMINDER",
                                                              "recipients": [{"contact_id": again["id"], "patent_ids": [p["id"]]}]}).json()
    env.client.post(f"/campaigns/{camp['id']}/submit", headers=admin)
    res = env.client.post(f"/campaigns/{camp['id']}/send", headers=admin).json()
    assert "SUPPRESSED" in res["communications"][0]["blocked_reasons"]


def test_template_approval_is_bound_to_content_hash(env):
    admin = env.login("admin@contoh.co.id")
    am = env.login("am@contoh.co.id")
    assert env.client.post("/templates/annuity_reminder/id/approve", headers=am,
                           json={"counsel_name": "Budi, S.H."}).status_code == 403
    r = env.client.post("/templates/annuity_reminder/id/approve", headers=admin,
                        json={"counsel_name": "Budi Hartono, S.H., M.H.", "notes": "Disetujui"})
    assert r.status_code == 200
    templates = {(t["key"], t["language"]): t for t in env.client.get("/templates", headers=admin).json()}
    assert templates[("annuity_reminder", "id")]["counsel_approved"] is True
    assert templates[("annuity_reminder", "en")]["counsel_approved"] is False


# ------------------------------------------------------------------ audit
def test_audit_chain_and_append_only(env):
    admin = env.login("admin@contoh.co.id")
    create_patent(env, admin)
    v = env.client.get("/audit/verify", headers=admin).json()
    assert v["audit_log"]["ok"] and v["audit_log"]["checked"] > 5 and v["agreement_acceptances"]["ok"]
    actions = {a["action"] for a in env.client.get("/audit", headers=admin).json()}
    assert {"patent.create", "agreement.accept", "auth.mfa_enrolled", "auth.password_ok"} <= actions
    with env.ctx.sessions() as db:
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("UPDATE audit_log SET action='x' WHERE id=1"))
        db.rollback()
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("DELETE FROM agreement_acceptance"))
        db.rollback()


def test_security_headers_and_health(env):
    r = env.client.get("/health")
    assert r.json() == {"status": "ok"}
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
