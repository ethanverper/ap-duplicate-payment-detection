# Stop paying the same supplier invoice twice

A file-driven AWS pipeline for accounts payable. It checks every ERP payment file the moment it lands, finds **duplicate payments** (even when the invoice number was typed differently or resubmitted under a new one), and reports **on-time payment, DPO and early-payment discounts lost** by entity, supplier category and supplier.

![CI](https://github.com/ethanverper/ap-duplicate-payment-detection/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC) ![AWS](https://img.shields.io/badge/AWS-S3%20events%20·%20Lambda%20·%20Glue%20Data%20Quality%20·%20Athena%20·%20SNS-FF9900)

> 🚧 **Work in progress.** Built in small, reviewed levels; each level is a separate set of commits. Second project of the portfolio, after [manufacturing-scrap-and-stockout-alerts](https://github.com/ethanverper/manufacturing-scrap-and-stockout-alerts). It is the same company, now seen from finance.

## Progress

| Level | What | Status |
|---|---|---|
| 0 | Business case, KPIs, planted data story, design, cost estimate ([docs/spec.md](docs/spec.md)) | ✅ Done |
| 1 | ERP simulator: 2 entities (MX/US), 16 suppliers, 90 days of daily payment files with control trailers, and the planted problems with an answer key | ✅ Done |
| 2 | File validation (trailer, schema, re-sent files) | ⏳ Next |
| 3 | Duplicate detection with confidence tiers + KPIs (PySpark), graded against the answer key | ⏳ |
| 4 | Terraform: S3 events → Lambda, Glue + Data Quality, Athena, SNS | ⏳ |
| 5 | Real deploy with evidence, cost report, teardown | ⏳ |

## 1. The business problem

Accounts payable pays thousands of supplier invoices a month, mostly keyed in by people. The same invoice gets paid twice in three common ways:
- it is typed again with a different number format (`FAC-0457` vs `fac 457`);
- the supplier resends it under a new number;
- an advance is paid and then the full amount anyway.

These errors are usually found months later, by an audit or a recovery firm. Separately, payment runs scheduled for the convenience of the calendar quietly throw away early-payment discounts worth ~37% a year.

**Who uses it:**
- AP analyst: stops or recovers a duplicate;
- Controller: fixes the process;
- Treasury: schedules payment runs;
- Procurement: talks to the suppliers.

## 2. KPIs

| KPI | Formula | Hierarchy | Alert | Frequency |
|---|---|---|---|---|
| **$ at risk from duplicate payments** | Amount paid in excess on suspected duplicates (USD) | Entity → Supplier category → Supplier → Invoice | High confidence → email AP the same day | Daily |
| **On-time payment %** | Paid by the due date ÷ all payments | Entity → Supplier category → Supplier | < 90% highlighted | Daily |
| **DPO** | Amount-weighted days from invoice to payment | Entity → Supplier category → Supplier | Report vs terms | Daily |
| **Early-payment discounts lost ($)** | Discount % × amount, for discount-term invoices paid after the window | Entity → Supplier category → Supplier → Invoice | Monthly total to Treasury | Daily |

## 3. The data (level 1)

The simulated ERP exports one CSV per entity per business day. **Its last line is a control trailer** with the record count and the total paid, the way bank and ERP files prove they arrived complete. 90 days, 2 entities, 6,322 payments, about $60M paid. Planted in it, with an answer key the pipeline will be graded against:

| Problem | Where | Count | $ at risk (USD) |
|---|---|---|---|
| Same invoice paid twice, number typed differently | Mexico | 12 | $115K |
| Invoice resubmitted by the supplier under a new number | US, a freight carrier | 8 | $23K |
| Advance paid, then the full amount anyway | Both | 6 | $21K |
| Payment runs only on Fridays, scheduled to due dates | US | all discount invoices | $369K of discounts lost |
| One supplier always paid late | Mexico | all its invoices | 0% on time |

There are also traps the detector must **not** fall into:
- monthly subscriptions that bill the same amount every month;
- dozens of legitimate advance + final payments.

## 4. Run it (so far)

Setup and every configuration value: [docs/configuration.md](docs/configuration.md)

```bash
make install && make test   # unit tests, no AWS
make generate               # 90 days of ERP files + master data + answer key into data/generated/
```

## Repository layout (so far)

```
config/            settings.yaml (every tunable value) · master_data.yaml (entities, suppliers, terms)
docs/              spec, configuration, coding standards
src/
  common/          the file contract (schema), settings, config, logging, AWS clients
  generators/      reference (master data, payment terms) · story (planted problems) · simulator · files · history
tests/             unit tests
```

Code standards: modular, typed, commented at every step. See [docs/CODING_STANDARDS.md](docs/CODING_STANDARDS.md).
