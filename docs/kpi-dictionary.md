# KPI dictionary

Every KPI is computed by the Glue job (`src/glue_jobs/transforms.py`) at its lowest grain, with additive counts and sums only. Rates are computed after rolling up, because a supplier's on-time % is not the average of its days. All money is in **USD** at the fixed rate in `config/settings.yaml` → `reporting.mxn_per_usd`.

**Hierarchy:** Entity (MX01, US01) → Supplier category → Supplier → Invoice.

## 1. $ at risk from duplicate payments

| | |
|---|---|
| **Business definition** | Money paid on a supplier invoice beyond what the invoice was for, or paid on an invoice that looks like a resubmission of one already paid |
| **Gold table** | `gold/duplicate_suspects`: one row per suspect payment, with `rule`, `confidence`, `amount_at_risk` (invoice currency) and `amount_at_risk_usd` |
| **HIGH confidence** (ADR-005) | Same entity, supplier and **normalized** invoice number (case, separators and leading zeros ignored) with total paid > invoice amount. The excess is at risk. The rule name says how: `SAME_INVOICE_RETYPED_NUMBER`, `ADVANCE_THEN_FULL_AMOUNT` or `SAME_INVOICE_PAID_TWICE`. |
| **MEDIUM confidence** (ADR-006) | Same entity and supplier, **exactly the same amount**, another number, invoice dates ≤ `detection.resubmission_window_days` apart. The later invoice's payment is at risk (`RESUBMITTED_UNDER_NEW_NUMBER`). |
| **Alert** | HIGH → email AP the same day (level 4). MEDIUM → the review list. |
| **Owner** | AP analyst (recover), controller (fix the process) |
| **Graded** | Against the simulator's answer key: precision and recall per kind (`make grade`, ADR-007) |

## 2. On-time payment %

| | |
|---|---|
| **Formula** | `on_time_payments / settling_payments` (count), or `on_time_paid_usd / paid_usd` (money) |
| **What counts** | Settling payments only (`FULL`, `FINAL`). An `ADVANCE` is early by design and is not judged. On time = `payment_date ≤ due_date`. |
| **Gold table** | `gold/payment_performance`, grain `dt · entity · category · supplier_id` |
| **Threshold** | A supplier below `kpis.on_time_alert_pct` (90%) is highlighted |
| **Owner** | AP manager, Procurement (supplier relationship) |

## 3. DPO, days payable outstanding

| | |
|---|---|
| **Formula** | `days_x_usd / paid_usd`: the amount-weighted average days from invoice date to payment date. A $50,000 invoice weighs 50 times more than a $1,000 one. |
| **Gold table** | `gold/payment_performance` (same grain) |
| **Reading it** | Compare it with the supplier's terms. Well below the net days means cash leaves early without a discount; above means paying late. |
| **Owner** | Treasury |

## 4. Early-payment discounts lost ($)

| | |
|---|---|
| **Formula** | For invoices with discount terms (e.g. `2/10 NET 30`) whose settling payment left **after** the discount deadline (invoice date + discount days): `discount_pct / 100 × invoice_amount_usd` |
| **Gold table** | `gold/discounts_lost`: one row per invoice that lost its discount |
| **Reading it** | A 2% discount for paying 20 days early is worth ~37% a year. Losses concentrated in one entity point to its payment calendar, not to the suppliers. |
| **Owner** | Treasury |

## Shared definitions

| Term | Definition |
|---|---|
| `dt` | The payment date (`payment_date`), the partition of every curated and gold table |
| Duplicate **row** | The same `payment_id` loaded twice. It is one payment: one copy is dropped. Counted in `gold/data_quality.duplicate_rows`. |
| Duplicate **payment** | Two different `payment_id`s for one supplier invoice: real money paid twice. It is what KPI 1 measures. |
| Quarantined row | A payment that breaks a business rule (unknown supplier for the entity, currency ≠ entity currency, non-positive amount, due or payment date before the invoice date, unknown payment type). Set aside with its reason in `quarantine/payments`; the rest of the day still loads. |
| Reconciliation | Every day: file rows = curated + duplicate rows + quarantined. Otherwise the job fails before writing. |
