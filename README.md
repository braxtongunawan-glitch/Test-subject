# IDPatent — Indonesian Patent Expiry, Annuity & Working Statement Tracker

A backend service for Indonesian patent portfolios: it normalizes DGIP numbers, computes protection terms, *biaya tahunan* (annuity) schedules and the annual Working Statement (*pernyataan pelaksanaan paten*) obligation, raises staged alerts, and sends compliant client correspondence in Bahasa Indonesia and English with UU PDP / UU ITE safeguards built into the send path.

> **Legal status of this software.** The legal parameters ship as *unverified* data compiled from practitioner commentary, and the agreement texts and e-mail templates are *drafts*. Production use requires review by Indonesian counsel: rule sets carry a verification status, templates need a recorded counsel approval bound to their content hash, and draft agreements cannot be published in production. See [docs/LEGAL_RULES.md](docs/LEGAL_RULES.md).

## What it does

| Area | Highlights |
|---|---|
| Number handling | `P00201912345`, `S00…`, historic PCT `W00…`, `IDP000012345`, `IDS…`, legacy `ID 0 012 345`, `PCT/US2019/012345`, `WO2019/123456`; separators, case and dropped leading zeros tolerated, with warnings |
| Calculations | 20-year Paten / 10-year Paten Sederhana terms; first annuity within 6 months of grant covering years 1…grant-year+1; later annuities 1 month before each filing anniversary; grace period and surcharge; Working Statements per calendar year (or per annuity cycle, configurable) |
| Effective-dated rules | Law 14/2001 (term), Law 13/2016, Law 65/2024 as JSON rule sets; each date uses the rule in force on that date (e.g. a 2022 installment keeps the 12-month on-request grace, a 2027 one gets the automatic 6-month grace) |
| Provenance | Every patent carries source (PDKI, SAKI, agent feed, manual verification, user input, supplementary), last-verified date, verification status, field-level manual overrides with reasons, and a 0–1 confidence score with explanatory warnings |
| Ingestion | CSV / XLSX / JSON bulk upload with Indonesian or English headers and Indonesian dates (`15 Maret 2019`, `15/03/2019`); per-row error report; PDKI *assisted* lookup; authorized JSON agent feeds; lower-priority sources cannot overwrite verified data |
| Owner matching | Indonesian and foreign legal forms, academic titles, pre-1972 spelling (`Gadjah`/`Gajah`), institutional aliases (ITB, UGM, BRIN…), affiliate guard, explainable scores and a review queue |
| Alerts | Expiry 18/12/6/3/1 months, annuity, grace end, unverified payment, Working Statement due/overdue, status and ownership changes, lapse risk; idempotent daily run, stage supersession, firm-configurable lead times |
| Correspondence | Bahasa Indonesia / English templates; enforced footer (legal name, physical address, purpose, lawful basis, privacy link, one-click opt-out); RFC 8058 `List-Unsubscribe`; thread and delivery tracking via signed ESP webhook |
| Compliance gate | Suppression list, purpose-level opt-out and consent ledger, lawful basis per UU PDP Art. 20, Legitimate Interest Assessments with four-eyes approval and review dates, cold-outreach restrictions (institutional addresses only, patent-specific, daily cap), review queue with four-eyes approval, counsel-approved templates in production |
| Click-wrap | Versioned ToS / Privacy Policy / third-party data notice / optional marketing consent; explicit per-document acceptance with the hash of the text shown; material changes force re-acceptance; hash-chained, append-only acceptance records with WIB timestamp, IP and user agent |
| Security | scrypt passwords with complexity and history, mandatory TOTP MFA with replay protection, lockout, idle and absolute session timeouts, HttpOnly/SameSite=Strict cookies plus CSRF header check, RBAC with separation of duties, AES-256-GCM field encryption with blind indexes, hash-chained append-only audit log enforced by DB triggers, security headers, tenant isolation |
| Data subject rights | DSAR export, erasure that keeps only a keyed hash on the suppression list, preference centre |
| Reporting | Dashboard (by type, status, owner type, confidence; annuity pipeline; Working Statement risk; data freshness) and localized CSV export with formula-injection protection; client portal scoped to the client's own owners |

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                                   # 120 tests

idpatent gen-keys > .env                 # IDPATENT_DATA_KEY / INDEX_KEY / TOKEN_KEY
set -a && . ./.env && set +a
export IDPATENT_DATABASE_URL=sqlite:///./idpatent.db
idpatent init --admin-email admin@firma.co.id --admin-name "Admin"   # seeds DRAFT agreements (non-production only)
idpatent serve                           # http://127.0.0.1:8000/docs
idpatent run-alerts                      # schedule daily, e.g. 06:00 WIB
```

First login: `POST /auth/login` → `POST /auth/mfa/enroll` (scan the `otpauth://` URI) → `POST /auth/mfa/verify` → `GET /agreements/pending` → `POST /agreements/accept`. Every data endpoint returns `403 AGREEMENT_ACCEPTANCE_REQUIRED` until current agreements are accepted.

### Key configuration (`IDPATENT_*`)

| Variable | Purpose |
|---|---|
| `ENV` | `development` / `test` / `production` (production enforces keys, HTTPS base URL, webhook secret, counsel-reviewed agreements and approved templates) |
| `DATABASE_URL` | SQLAlchemy URL (SQLite for development; PostgreSQL recommended for production) |
| `DATA_KEY`, `INDEX_KEY`, `TOKEN_KEY` | 32-byte base64 keys: field encryption, blind index, opt-out link signing |
| `PUBLIC_BASE_URL`, `PRIVACY_URL` | Links placed in e-mails (must be HTTPS) |
| `EMAIL_PROVIDER` + `SMTP_*` | `outbox` (captures, default) or `smtp` (TLS 1.2+ required) |
| `ESP_WEBHOOK_SECRET` | HMAC-SHA256 secret for `POST /webhooks/esp` (`X-IDPatent-Signature: sha256=…`) |
| `RULES_DIRECTORY` | Load counsel-reviewed rule sets from a directory instead of the packaged ones |

## Layout

```
src/idpatent/
  domain/          numbers, dates (WIB, Indonesian formats), rules, calculator, matching, alerts, i18n
  rules/           effective-dated legal rule sets (JSON) + default alert policy
  compliance/      send gate, click-wrap logic, audit hash chain, signed tokens
  notifications/   Jinja templates (id/en), renderer with enforced footer, ESP providers
  security/        passwords, TOTP, AES-GCM field encryption, RBAC
  ingestion/       CSV/XLSX/JSON parsing, PDKI-assisted lookup, verified datasets, agent feeds
  db/              SQLAlchemy models, append-only triggers
  services/        patents, contacts, communications, agreements, auth, reports, batch jobs
  api/             FastAPI app and routers (staff, client portal, public opt-out pages)
  agreements/      DRAFT agreement texts (id/en)
docs/              architecture, legal rules, SRS traceability, operations
```

## Documentation

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, key design decisions
* [docs/LEGAL_RULES.md](docs/LEGAL_RULES.md): what the rule sets encode, sources, open questions for counsel
* [docs/TRACEABILITY.md](docs/TRACEABILITY.md): SRS requirement → implementation → tests, including what is not built
* [docs/OPERATIONS.md](docs/OPERATIONS.md): deployment, keys, e-mail authentication, backups, incident response
