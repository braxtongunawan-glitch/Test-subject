import base64
import os
import time
from dataclasses import dataclass, field
from datetime import date

import pytest
from fastapi.testclient import TestClient

from idpatent.api.app import create_app
from idpatent.config import Settings
from idpatent.db.models import Firm
from idpatent.domain.enums import Role
from idpatent.notifications.esp import OutboxProvider
from idpatent.security import totp
from idpatent.services import agreements, auth
from idpatent.services.audit import Actor
from idpatent.services.context import build_context

PASSWORD = "Rahasia#Kuat2026"


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@dataclass
class Env:
    client: TestClient
    ctx: object
    outbox: OutboxProvider
    clock: list
    firm_id: int
    other_firm_id: int
    secrets: dict = field(default_factory=dict)
    logins: dict = field(default_factory=dict)

    def set_today(self, d: date) -> None:
        self.clock[0] = d

    def login(self, email: str, *, accept: bool = True) -> dict:
        """Full password + TOTP login; returns auth headers. Accepts pending agreements by default."""
        c = self.client
        r = c.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        challenge = r.json()["challenge"]
        if r.json()["status"] == "MFA_ENROLLMENT_REQUIRED":
            self.secrets[email] = c.post("/auth/mfa/enroll", json={"challenge": challenge}).json()["secret"]
        n = self.logins.get(email, 0)
        self.logins[email] = n + 1
        code = totp.totp(self.secrets[email], at=time.time() + 30 * n)  # fresh step per login (replay protection)
        r = c.post("/auth/mfa/verify", json={"challenge": challenge, "code": code})
        assert r.status_code == 200, r.text
        headers = {"Authorization": f"Bearer {r.json()['token']}"}
        if accept:
            pending = c.get("/agreements/pending", headers=headers).json()
            r = c.post("/agreements/accept", headers=headers, json={"items": [
                {"document_id": d["document_id"], "accepted": True, "content_hash": d["content_hash"]} for d in pending]})
            assert r.status_code == 200, r.text
        return headers


@pytest.fixture
def env() -> Env:
    settings = Settings(
        environment="test", database_url="sqlite://", data_key_b64=_key(), index_key_b64=_key(), token_key_b64=_key(),
        cookie_secure=False, public_base_url="https://app.test", esp_webhook_secret="whsec-test",
    )
    clock = [date(2026, 11, 22)]
    outbox = OutboxProvider()
    ctx = build_context(settings, email=outbox, today=lambda: clock[0])
    client = TestClient(create_app(ctx))
    with ctx.sessions() as db:
        agreements.seed_default_agreements(db, Actor.system(), production=False)
        firm = Firm(name="Contoh", legal_name="PT Contoh Konsultan HKI", address="Jl. Jend. Sudirman Kav. 1, Jakarta 10220",
                    email="hki@contoh.co.id", phone="+62 21 555 0100",
                    services_id=["Pengelolaan pembayaran biaya tahunan", "Bantuan pernyataan pelaksanaan paten"],
                    services_en=["Annuity payment management", "Working statement assistance"])
        other = Firm(name="Lain", legal_name="PT Firma Lain", address="Jl. Gatot Subroto No. 2, Jakarta 12930",
                     email="info@lain.co.id")
        db.add_all([firm, other])
        db.flush()
        for email, role, fid in [
            ("admin@contoh.co.id", Role.FIRM_ADMIN, firm.id),
            ("compliance@contoh.co.id", Role.FIRM_ADMIN, firm.id),
            ("am@contoh.co.id", Role.ACCOUNT_MANAGER, firm.id),
            ("viewer@contoh.co.id", Role.VIEWER, firm.id),
            ("admin@lain.co.id", Role.FIRM_ADMIN, other.id),
        ]:
            auth.create_user(db, ctx, email=email, name=email.split("@")[0], role=role, password=PASSWORD, firm_id=fid)
        db.commit()
        firm_id, other_id = firm.id, other.id
    return Env(client, ctx, outbox, clock, firm_id, other_firm_id=other_id)
