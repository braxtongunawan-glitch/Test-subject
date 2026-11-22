"""Command-line entry point: bootstrap, key generation, scheduled runs.

Examples::

    idpatent gen-keys                       # print fresh base64 keys for the environment
    idpatent init --admin-email a@firma.co.id --admin-name "Admin"
    idpatent run-alerts                     # daily job (e.g. cron at 06:00 WIB)
    idpatent verify-audit
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from sqlalchemy import select

from .config import Settings
from .db.models import Firm
from .domain.enums import Role
from .security.crypto import generate_key_b64
from .services import agreements, audit, auth, batch
from .services.audit import Actor
from .services.context import build_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="idpatent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-keys", help="Generate encryption, index and token keys")
    init = sub.add_parser("init", help="Create schema, seed draft agreements, create the first super admin")
    init.add_argument("--admin-email", required=True)
    init.add_argument("--admin-name", required=True)
    run = sub.add_parser("run-alerts", help="Recompute all patents and create alerts (daily job)")
    run.add_argument("--as-of", help="ISO date; defaults to today in WIB")
    sub.add_parser("verify-audit", help="Verify the audit and acceptance hash chains")
    serve = sub.add_parser("serve", help="Run the API with uvicorn (development)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.cmd == "gen-keys":
        for name in ("IDPATENT_DATA_KEY", "IDPATENT_INDEX_KEY", "IDPATENT_TOKEN_KEY"):
            print(f"{name}={generate_key_b64()}")
        return 0

    settings = Settings.from_env()
    ctx = build_context(settings)

    if args.cmd == "init":
        import os

        password = os.environ.get("IDPATENT_ADMIN_PASSWORD") or getpass.getpass("Password for the super admin: ")
        with ctx.sessions() as session:
            actor = Actor.system("cli:init")
            if not settings.production:
                agreements.seed_default_agreements(session, actor, production=False)
            auth.create_user(session, ctx, email=args.admin_email, name=args.admin_name, role=Role.SUPER_ADMIN,
                             password=password, firm_id=None, actor=actor)
            session.commit()
        print("Initialised. Log in and enrol an authenticator app to complete MFA setup.")
        return 0

    if args.cmd == "run-alerts":
        from datetime import date

        as_of = date.fromisoformat(args.as_of) if args.as_of else ctx.today()
        results = {}
        with ctx.sessions() as session:
            for firm in session.execute(select(Firm)).scalars().all():
                results[firm.id] = batch.run_daily(session, ctx, firm, as_of)
            session.commit()
        print(json.dumps(results, indent=2))
        return 0

    if args.cmd == "verify-audit":
        with ctx.sessions() as session:
            a = audit.verify(session)
            b = agreements.verify_acceptance_chain(session)
        print(json.dumps({"audit_log": a.__dict__, "agreement_acceptances": b.__dict__}, default=str, indent=2))
        return 0 if a.ok and b.ok else 1

    if args.cmd == "serve":
        import uvicorn

        from .api.app import create_app

        uvicorn.run(create_app(ctx), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
