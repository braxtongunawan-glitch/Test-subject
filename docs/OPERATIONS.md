# Operations

## Deployment checklist

1. **Database**: PostgreSQL with encryption at rest, automated encrypted backups, point-in-time recovery. Define RPO/RTO and test a restore before go-live. The append-only trigger for PostgreSQL is created by `create_schema`; verify it in your environment.
2. **Keys**: generate with `idpatent gen-keys`; store in a secrets manager or KMS, never next to the database or its backups. Losing `IDPATENT_DATA_KEY` makes contact data unrecoverable; losing `IDPATENT_INDEX_KEY` disables suppression look-ups. Rotating either requires a re-encryption job (not included).
3. **TLS**: terminate TLS 1.2+ (prefer 1.3) at the load balancer; set `IDPATENT_PUBLIC_BASE_URL=https://…`. HSTS is sent in production.
4. **WAF and rate limits** in front of the API. The in-app limiter only covers login and MFA and is per process.
5. **Run** with `IDPATENT_ENV=production`. Startup fails without keys, webhook secret or an HTTPS base URL. In production, draft agreements cannot be published and unapproved templates cannot be sent.
6. **Schedule** `idpatent run-alerts` daily (for example 06:00 WIB).
7. **Legal sign-off** before first send: counsel-approved agreement texts (`publish_version(..., counsel_reviewed=True)`), template approvals (`POST /templates/{key}/{lang}/approve`), verified rule sets (see LEGAL_RULES.md).

## E-mail authentication

* Send only through an ESP relay on a domain you control, with **SPF** (`v=spf1 include:<esp> -all`), **DKIM** (2048-bit, ESP-managed selector) and **DMARC** (start with `p=quarantine`, add `rua` reporting, move to `p=reject`).
* Configure the ESP's event webhook to `POST https://<host>/webhooks/esp` with an HMAC-SHA256 signature of the raw body in `X-IDPatent-Signature: sha256=<hex>`. Payload: `{"event": "delivered|opened|replied|bounced|complained|unsubscribed", "message_id": "<…>", "bounce_type": "hard|soft"}` (one object or a list).
* Watch bounce and complaint rates. Hard bounces are suppressed for the firm, complaints platform-wide.

## Data subject requests (UU PDP)

* Access: `GET /contacts/{id}/dsar-export` (compliance role); acceptance history for users: `GET /agreements/history`.
* Erasure: `POST /contacts/{id}/erase` clears name, phone and e-mail and keeps only the keyed hash on the suppression list.
* Withdrawal / objection: preference centre link in every e-mail, or `POST /contacts/{id}/opt-out` / `…/consent` by staff.

## Incident response (outline)

1. Detect (SIEM alerts on auth failures, export spikes, audit-chain verification failures from `idpatent verify-audit`).
2. Contain: revoke sessions (a password change revokes all of that user's sessions), rotate keys if exposed, disable affected accounts.
3. Assess whether personal data was compromised; record decisions in the incident log.
4. Notify affected data subjects and the authority within **3 × 24 hours** where UU PDP requires.
5. Post-incident review; run tabletop exercises at least annually.

## Monitoring

* `GET /health` for liveness.
* Run `idpatent verify-audit` daily and alert on a non-zero exit code.
* Track alert backlog (`GET /alerts?severity=CRITICAL`), blocked communications (`GET /communications?status=BLOCKED`), and patents with stale verification (dashboard `data_freshness`).
