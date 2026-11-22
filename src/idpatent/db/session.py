"""Engine/session setup and append-only enforcement for audit tables."""

from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base

APPEND_ONLY_TABLES = ("audit_log", "agreement_acceptance", "consent_event")


def make_engine(url: str) -> Engine:
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url == "sqlite://":
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            # Let SQLAlchemy manage transactions so SAVEPOINTs (per-row import isolation) work.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        @event.listens_for(engine, "begin")
        def _begin(conn):  # pragma: no cover - trivial
            conn.exec_driver_sql("BEGIN")
    return engine


def create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            for table in APPEND_ONLY_TABLES:
                for op in ("UPDATE", "DELETE"):
                    conn.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} "
                            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
                        )
                    )
        elif engine.dialect.name == "postgresql":  # pragma: no cover - exercised in PG deployments
            conn.execute(
                text(
                    "CREATE OR REPLACE FUNCTION idp_append_only() RETURNS trigger AS $$ "
                    "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql"
                )
            )
            for table in APPEND_ONLY_TABLES:
                conn.execute(text(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}"))
                conn.execute(
                    text(
                        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                        "FOR EACH ROW EXECUTE FUNCTION idp_append_only()"
                    )
                )


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
