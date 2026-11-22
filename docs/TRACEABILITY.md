# SRS traceability

Status: **Done** = implemented and tested · **Partial** = implemented in part (gap stated) · **Not built** = outside this delivery (reason stated).

## 3.1 Patent number ingestion & matching

| Requirement | Status | Implementation | Tests |
|---|---|---|---|
| Application, registration, Paten vs Paten Sederhana, PCT national phase formats | Done | `domain/numbers.py` | `test_dates_numbers.py` |
| Normalization incl. historical variants | Done | separators, case, missing `00`, zero-padding, legacy `ID…`, `W` prefix, 2-digit-year PCT | `test_parse_numbers`, `test_legacy_registration` |
| Bulk upload CSV / Excel / JSON and single lookup | Done | `ingestion/parsers.py`, `POST /patents/import`, `POST /patents`, `GET /lookup` | `test_csv_import…`, `test_xlsx_import`, `test_lookup…` |
| Match against PDKI | Partial | Assisted: PDKI search link + `POST /lookup/pdki-confirm` records operator-confirmed fields as source PDKI. No automated extraction (no public API; terms of use unconfirmed) | `test_pdki_assisted_lookup` |
| Fuzzy owner matching with confidence | Done | `domain/matching.py`, review queue `GET /owners?needs_review=true`, `POST /owners/{id}/resolve` | `test_matching_alerts.py` |
| Family linking (priority, PCT) | Partial | Priority claims, PCT application/publication numbers and `family_id` stored; no automatic family retrieval from EPO OPS | `test_priority_parsing` |

## 3.2 Expiry, annuity & Working Statement

| Requirement | Status | Implementation | Tests |
|---|---|---|---|
| 20 / 10-year terms | Done | rule sets + `calculator.py` | `test_standard_patent_schedule`, `test_simple_patent_schedule`, `test_leap_day_filing` |
| First annuity within 6 months of grant; annual thereafter 1 month before anniversary | Done | `_annuity_schedule` | same |
| 6-month grace with surcharge | Done | per-installment rule selection (12-month on request before 28 Oct 2024) | `test_in_grace_period` |
| Working Statement (Law 65/2024) | Done | calendar-year (default) or annuity-cycle mode | `test_working_statements_calendar_mode`, `test_with_annuity_mode_is_configurable` |
| Store / display end date, next annuity + grace, WS due, status, confidence, source, last verified | Done | `Patent` cache fields, `services/views.py` | `test_create_patent_and_detail` |
| Data-source priority; manual override | Done | `SOURCE_RANK`, `apply_override` (reason required, audited) | `test_lower_priority_source…`, `test_override_payment…` |
| Supplementary sources only for priority/family | Done | `SUPPLEMENTARY` records may only update family fields and cannot create patents | `upsert_patent` |

## 3.3 Alerting

| Requirement | Status | Implementation | Tests |
|---|---|---|---|
| Expiry 18/12/6/3/1 months, annuity + grace, WS deadline, status / ownership change, lapse risk | Done | `domain/alerts.py`, `services/patents.persist_alerts` | `test_alert_stages_and_dedupe`, `test_status_change_alert_on_import` |
| Configurable lead times | Done | firm `alert_policy` (`PUT /admin/firm`) | `test_alert_policy_validation` |
| Dashboard + e-mail to account managers | Partial | Alerts via API/dashboard. Internal alert digests to account managers are not e-mailed yet (the ESP layer is ready) | — |
| Optional client notification | Done | campaigns (`auto_within_days` builds recipients from due obligations) | `test_client_annuity_reminder_end_to_end` |

## 3.4 Correspondence

| Requirement | Status | Implementation | Tests |
|---|---|---|---|
| Owners, representatives, contacts | Done | `Owner`, `Contact` (relationship CLIENT / PROSPECT / AGENT), agent name on patents | API tests |
| Bahasa Indonesia primary, English secondary | Done | templates, labels, dates, CSV headers, opt-out pages, agreements; `?lang=en` | `test_every_template_renders_with_footer` |
| Templated notices + service offers | Done | five templates × two languages; firm-configurable service lines | same |
| Thread tracking, replies, sent/opened/replied/opted-out | Done | `Message-ID`/`thread_id`, signed ESP webhook | `test_client_annuity_reminder_end_to_end` |
| Portfolio view per client; client portal + preferences | Done | `/portal/*` scoped by `owner_scope` | `test_client_portal_scope` |

## 3.5 Reporting & administration

| Requirement | Status | Implementation | Tests |
|---|---|---|---|
| Indonesia-specific dashboard | Done | `GET /reports/dashboard` | `test_dashboard_and_csv_export` |
| Exports in Bahasa Indonesia / English | Done | `GET /reports/portfolio.csv` | same |
| RBAC, audit of data access and outbound communications | Done | `security/rbac.py`, `services/audit.py` | `test_rbac_separation_of_duties`, `test_audit_chain_and_append_only` |

## 4 Non-functional

| Requirement | Status | Notes |
|---|---|---|
| Provenance & manual override | Done | see 3.2 |
| Full Bahasa Indonesia **UI** | Partial | The API, e-mails, reports and public pages are localized. A staff web front end is **not built**; the API returns ready-to-display Indonesian labels for one |
| Reliability (freshness, verification workflow) | Done | confidence, `VERIFICATION_AGING`/`STALE`, `POST /patents/{id}/verify` |
| Security & audit | Done | see section 10 |
| Performance (hundreds to low thousands of patents) | Done | single-pass recompute; owner matching uses token blocking. Not load-tested |

## 5 Compliance

| Requirement | Status | Implementation |
|---|---|---|
| Lawful basis documented; LIA where used | Done | contact `lawful_basis`, `basis_note` required for prospects, LIA with four-eyes approval and review date |
| Sender identity + physical address in every e-mail | Done | enforced footer; gate blocks if firm identity incomplete |
| One-click opt-out honoured promptly | Done | RFC 8058 POST + confirmation page; gate checks live state |
| Global suppression list | Done | firm-level and platform-wide entries; complaints are platform-wide |
| Review / approval queue for bulk or cold | Done | `requires_approval`, four-eyes review, reviewer note for cold campaigns |
| Prefer institutional e-mails | Done | cold outreach to personal webmail is blocked |
| Counsel review of templates | Done | approvals bound to template content hash; required in production |

## 10 Security measures

| Requirement | Status | Notes |
|---|---|---|
| MFA mandatory | Partial | TOTP authenticator apps, enforced for every role. SMS / e-mail OTP not built (TOTP is stronger) |
| RBAC, least privilege | Done | five roles, separation of duties |
| Sessions, cookies, lockout, password policy | Done | idle 30 min / absolute 12 h, HttpOnly + Secure + SameSite=Strict, CSRF header, 5-attempt lockout, complexity + history |
| SSO (SAML / OIDC) | Not built | Next step for institutional clients; session layer is ready to accept an external identity |
| Encryption at rest / in transit | Partial | Field-level AES-256-GCM in-app. Disk / DB / backup encryption and TLS termination are deployment controls (see OPERATIONS.md) |
| Key management | Partial | `KeyProvider` interface; environment provider shipped, KMS adapter to be added |
| WAF, rate limiting | Partial | in-app rate limits on auth endpoints; WAF is a deployment control |
| Immutable audit logs | Done | DB triggers + hash chains + verification endpoint |
| SSDLC, pen testing, SIEM, DR, incident response, tabletop exercises | Not built (process) | Checklists in OPERATIONS.md |
| ESP with SPF/DKIM/DMARC; no executable attachments; HTTPS links | Done / deployment | DNS setup in OPERATIONS.md; attachment and link checks in code |

## 11 Click-wrap

| Requirement | Status | Implementation |
|---|---|---|
| ToS, Privacy Policy, third-party data notice, optional marketing consent, optional client agreement | Done | `services/agreements.py`; drafts in `agreements/` (**must be replaced by counsel-approved texts**) |
| Unticked checkboxes, explicit affirmative action | Done | `checked: false` in API; each document needs `accepted: true` |
| Version, hash, WIB timestamp, IP, user agent, account, language | Done | `AgreementAcceptance` |
| Re-acceptance on material change | Done | `test_material_policy_update_forces_reacceptance` |
| Organization acceptance by representative | Done | `on_behalf_of_firm_id` recorded for firm admins and client users |
| Export acceptance history; DSAR | Done | `GET /agreements/history`, `GET /contacts/{id}/dsar-export` |
| Withdrawal of consent stops processing | Done | consent ledger checked at send time |

## 8 Phases

Phase 1 (core engine) and Phase 2 (correspondence and compliance) are implemented. Phase 3 is partly implemented (client portal, owner resolution, reporting). Phase 4 (commercialization / IP Bridge flags, multi-portfolio analytics) is not built.
