# Legal rule sets and open questions for counsel

All legal parameters live in `src/idpatent/rules/id_*.json`. Each file is an effective-dated rule set with its own citations and a `verification.status`. Every computation lists the rule sets it used, and the warning `RULES_NOT_COUNSEL_VERIFIED` appears on every patent until each rule set used is marked `VERIFIED`.

**To sign off:** have Indonesian counsel review each file against the official text, correct values, set `verification.status` to `VERIFIED` with `reviewed_by` / `reviewed_on`, and deploy via `IDPATENT_RULES_DIRECTORY` or a release. Changing a value changes the registry fingerprint stored on each patent (`rules_version`), so recomputed results can be traced to the rule content that produced them.

## How a rule set is chosen

| Calculation | Date used to select the rule set |
|---|---|
| Protection term (20/10 years) | Filing date (*Tanggal Penerimaan*) |
| First annuity timing | Grant date |
| Later annuity timing | Start of the protection year being paid |
| Grace period and surcharge | The installment's due date |
| Working Statement | 31 December of the reporting year (calendar mode) or the installment due date (annuity mode) |

## Rule sets shipped

| ID | Effective | Encodes | Notes |
|---|---|---|---|
| `ID-UU14-2001` | 2001-08-01 | Term only: Paten 20 y, Paten Sederhana 10 y | Annuity timing under Law 14/2001 is **not modelled**. Installments that fall in this era use the earliest later rule set and are flagged `ANNUITY_RULES_APPROXIMATED` (only while not marked paid). Filings before 2001-08-01 fall back with `RULES_FALLBACK_PRE_2001`. |
| `ID-UU13-2016` | 2016-08-26 | Term; first annuity within 6 months of grant covering year 1 … grant year + 1; later annuities 1 month before the filing anniversary; grace 12 months **on request**, 100% surcharge | Sources describe the late-payment window differently (6 months extendable to 12 on notice, or 12 months on request). |
| `ID-UU65-2024` | 2024-10-28 | As above, with an **automatic 6-month** grace period and 100% surcharge; annual Working Statement due 31 December from reporting year 2024 | See open questions 1–4. |

Term end convention: `ANNIVERSARY` (end date = filing date + N years). `DAY_BEFORE_ANNIVERSARY` is available if counsel or PDKI practice indicates the term ends the day before. A 29 February filing date anniversaries on 28 February in non-leap years.

## Discrepancies between the SRS and published commentary

1. **Working Statement deadline.** The SRS says the statement is "due in conjunction with the annuity payment cycle". Practitioner commentary on Law 65/2024 (Rouse, Tilleke & Gibbins) reports that holders must file it **by the end of each year**. The shipped default is `CALENDAR_YEAR_END`; switching `working_statement.mode` to `WITH_ANNUITY` implements the SRS reading (one statement per installment, due on the installment due date). Both modes are tested.
2. **Article number.** The SRS cites "Article 20A". The shipped citation notes that the article number must be verified.

## Open questions for counsel

1. Grace period under Law 65/2024: automatic 6 months (most commentary) or 12 months on request (one published translation of Art. 126)? Is the surcharge 100% of the annual fee?
2. Working Statement: calendar-year or annuity-cycle deadline? Is a statement required for 2024 (the law took effect 28 October 2024) and for a final partial year of protection (flagged `WORKING_STATEMENT_FINAL_YEAR_VERIFY`)? What are the consequences of non-filing once the implementing regulation is issued?
3. First annuity: confirm "within 6 months of the certificate / grant date" and that it covers year 1 up to the grant year plus one further year.
4. Later annuities: confirm "no later than 1 month before the filing-date anniversary that starts the next protection year".
5. Transitional treatment of patents granted before 26 August 2016 whose first payment fell under Law 14/2001.
6. Term end date as displayed by PDKI: on the anniversary or the day before?
7. Annuity fee amounts (PNBP tariff regulation): not computed. Payments record the amount actually paid; a fee schedule can be added as another effective-dated data file.

## Status vocabulary

| Code | Bahasa Indonesia | Meaning |
|---|---|---|
| `PENDING` | Dalam Proses | No grant date |
| `GRANTED` | Diberi | In force |
| `EXPIRED` | Berakhir | Term ended (computed), or recorded as such |
| `LAPSED` | Dihapus | Recorded by an official source |
| `LAPSE_SUSPECTED` | Berisiko Dihapus | Computed: an installment's grace period ended with no recorded payment. Never asserted as a lapse; it prompts verification |
| `REFUSED` / `WITHDRAWN` | Ditolak / Ditarik Kembali | Recorded |

Recorded terminal statuses (`LAPSED`, `REFUSED`, `WITHDRAWN`) from a source always win over computation. When payment status is unknown, past installments are reported as `UNKNOWN` (not missed) and raise an `ANNUITY_PAYMENT_UNVERIFIED` alert during the grace window.

## Sources consulted (secondary)

* Rouse: Major Changes to Indonesia's New Patent Law (Law No. 65 of 2024)
* Tilleke & Gibbins: Indonesia Amends Patent Law; Annuity Payments under Indonesia's New Patent Law
* Am Badar: Amendments to Indonesian Patent Law
* Swanapada: Indonesia New Patent Law No. 65 of 2024
* Spruson & Ferguson: Indonesia update, clarification of annuity payments under the new patent law

These are commentary, not the official text. The official Indonesian text of Law 13/2016 as amended by Law 65/2024 prevails.
