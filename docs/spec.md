# Project spec (written BEFORE any code)

## 1. Business case
- **Industry / function:** Manufacturing, finance / accounts payable (AP). It is the same company as `manufacturing-scrap-and-stockout-alerts`: a Mexican manufacturer (plants in Monterrey and San Luis Potosí) with a US distribution entity.
- **Questions to answer:**
  - Are we paying any supplier invoice twice?
  - Are we paying on time, and are we losing early-payment discounts?
- **Who decides what with it:**

  | Role | Decision |
  |---|---|
  | AP analyst | Stop or recover a duplicate payment before or after it leaves the bank |
  | AP manager / controller | Fix the process that let it through (manual entry, supplier resubmissions) |
  | Treasury | Schedule payment runs so discounts are not lost and due dates are met |
  | Procurement | Talk to suppliers that resubmit invoices or are always paid late |

- **Why now / cost of not knowing:** duplicate payments are usually found months later, by an audit or by a recovery firm that keeps a share of what it recovers. By then the money is hard to get back. A missed "2/10 net 30" discount is 2% of the invoice, equal to a ~37% annualized return thrown away.

## 2. KPIs and hierarchy

| KPI | Formula | Dimensions (L1 → L2 → L3 → L4) | Threshold / alert | Frequency |
|---|---|---|---|---|
| **$ at risk from duplicate payments** | Sum of the amount paid in excess on suspected duplicates, in USD | Entity → Supplier category → Supplier → Invoice (+ duplicate rule, confidence) | Any **high-confidence** duplicate → email AP the same day. Medium confidence → review list. | Daily |
| **On-time payment %** | Payments made on or before the due date ÷ all payments (count and $) | Entity → Supplier category → Supplier | Report. Below 90% for a supplier is highlighted. | Daily |
| **DPO, days payable outstanding** | Amount-weighted average days from invoice date to payment date | Entity → Supplier category → Supplier | Report, against the agreed payment terms | Daily |
| **Early-payment discounts lost ($)** | For invoices with discount terms (e.g. 2/10 net 30) paid after the discount window: discount % × amount, in USD | Entity → Supplier category → Supplier → Invoice | Report. Total per month for Treasury. | Daily |

**Entities:**
- `MX01`: Mexico, MXN.
- `US01`: United States, USD.

All money is reported in USD at a fixed rate from `config/settings.yaml`, so totals add up across entities.

**Supplier categories:**
- Direct materials: the four parts suppliers from P1;
- Logistics;
- Services and maintenance;
- IT and utilities.

## 3. Data
- **How it arrives:** the ERP exports **one AP file per entity per day** (around 06:00) to S3:
  - path: `landing/ap/entity=<ENTITY>/<ENTITY>_ap_<YYYYMMDD>.csv`;
  - each file holds the day's payments with the invoice they settle;
  - **the last line is a control trailer**: record count and total amount, the way bank and ERP files prove they arrived complete.
- **Volume:** 90 days of history × 2 entities × ~40 payments a day ≈ **7,000 payments** from ~25 suppliers. Plus one "today" file per entity for the live demo.
- **Sample record:**
```csv
payment_id,entity,supplier_id,supplier_invoice_number,invoice_date,due_date,invoice_amount,currency,payment_terms,payment_date,paid_amount,payment_type
PAY-MX01-000457,MX01,SUP-PRECISA,FAC-0457,2026-09-02,2026-10-02,18450.00,MXN,2/10 NET 30,2026-09-26,18450.00,FULL
TRAILER,2,,,,,,,,,36900.00,
```
- **The story planted in the data:**

  | # | Problem | Where | What the pipeline must do |
  |---|---|---|---|
  | 1 | **Same invoice number, different format** (`FAC-0457`, `FAC0457`, `fac 457`) entered twice | MX01, manual entry | Normalize the number. Flag as **high confidence**. |
  | 2 | **Resubmitted invoice:** same supplier, same amount, invoice date within a few days, **new number** | US01, a logistics carrier | Flag as **medium confidence** (review). It must not fire on a supplier's normal recurring same-amount invoices (e.g. a monthly fee). |
  | 3 | **Partial + total:** an advance (`ADVANCE`) is paid, then the **full** invoice amount is paid anyway | Both entities, a few cases | Flag the overpayment. **Legitimate** advance + final payments that add up to the invoice (many more cases) must **not** be flagged. |
  | 4 | Payments run only on Fridays | US01 | Shows up as discounts lost on 2/10 suppliers |
  | 5 | One supplier chronically paid late | MX01 | Shows up in on-time % |

- **Known data issues to handle:**
  - a file whose trailer doesn't match its rows → **rejected** and AP alerted, never loaded;
  - the same file uploaded twice → processed once (idempotent, by file checksum);
  - a missing column or a bad date → the file is rejected with the reason;
  - amounts in two currencies.

## 4. Design
- **Pattern:** batch, file-driven, with immediate validation.

  ```
  ERP file → S3 landing/ ─(S3 event)→ Lambda: validate file + trailer ─┬→ S3 validated/   → Glue (PySpark + Data Quality) → gold/ → Athena
                                                                        └→ S3 rejected/ + SNS email to AP
                                                         Glue: high-confidence duplicates → SNS email to AP
  ```

- **Services and why:**

  | Service | Role | Why this one |
  |---|---|---|
  | S3 + event notifications | Landing zone; each new file triggers validation | Files are how ERPs and banks hand off data. The event removes polling. |
  | Lambda | File-level checks in seconds: header, columns, trailer count and total, checksum | Cheap and instant; a bad file is caught before anyone builds on it |
  | Glue (PySpark) + **Glue Data Quality** | Row-level rules (completeness, ranges, referential integrity) declared in DQDL; duplicate detection; KPI tables | DQ rules become a declared, versioned artifact, with results stored next to the data |
  | Athena | KPI queries with `ROLLUP`, the duplicate review list | Same pattern as P1; pay per query |
  | SNS | Rejected-file and duplicate-payment emails to AP | Same as P1 (filtered subscriptions) |
  | Terraform | Everything as code, settings from `config/settings.yaml` | Same as P1 |

- **New concepts this project introduces:**
  1. **Event-driven file ingestion:** S3 event → Lambda, control-total validation, a quarantine for rejected files, idempotency by checksum.
  2. **Glue Data Quality (DQDL)** and **fuzzy duplicate matching** (normalized invoice numbers, amount and date windows, confidence tiers).
- **Reused from P1:** Glue job structure and local Spark tests, Athena ROLLUP queries, SNS filtered alerts, evidence and teardown scripts.
- **Depth:** Light. No streaming and nothing billed by the hour.

## 5. Cost estimate (goal < $1, hard cap $3)

| Service | Unit price (us-east-1) | Usage during the demo | Estimate |
|---|---|---|---|
| Glue (Spark) + Data Quality | $0.44 per DPU-hour, 1-minute minimum | 2 DPU × ~3 min × 2 runs | $0.09 |
| Lambda | $0.20 per million requests + compute | ~200 file validations | ~$0.00 (free tier) |
| S3 | $0.023 per GB-month + requests | ~5 MB for a few hours | ~$0.00 |
| Athena | $5 per TB, 10 MB minimum per query | ~20 queries | ~$0.00 |
| SNS email | first 1,000 free | ~10 emails | $0.00 |
| CloudWatch Logs | $0.50 per GB ingested | a few MB, 1-day retention | ~$0.00 |
| **Total** | | | **≈ $0.10** |

- **Nothing in this design bills by the hour.** Forgetting the destroy would cost cents, not dollars. The plan is still deploy → evidence → destroy in one window of ≤ 1 hour.
- Scheduled triggers off by default.

## 6. Definition of done
- [ ] Tests and CI green
- [ ] Deployed for real; screenshots and results in `docs/images/` and README section 3
- [ ] `make destroy` + `make leftovers` empty
- [ ] Actual cost recorded in the README (≤ $3)
- [ ] README: all sections filled, readable by a non-technical person
- [ ] CV bullet drafted (portfolio project, described truthfully)
