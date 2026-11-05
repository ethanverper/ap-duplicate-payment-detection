# Deploy log

The real deploy of this project to AWS: what was created, what was checked at each step, what happened, what it cost, and proof that everything was deleted afterwards.

- **Date:** 2026-11-05 (UTC) · **Region:** us-east-1
- **Window:** 01:16:05 → 01:45:32 UTC, **29 min 27 s**. Nothing in this design bills by the hour.
- **Estimated cost:** ≈ $0.10 (`docs/spec.md`, section 5) · **Actual cost:** section 5
- **Evidence:**
  - CLI output in `docs/evidence/<phase>/` (account id and emails redacted);
  - 14 console screenshots in `docs/images/console/`, cropped so no account or personal detail appears;
  - Athena results in `docs/images/results/`.

## 1. Timeline

| Time (UTC) | Step | Command | Expected | Result |
|---|---|---|---|---|
| 01:06 | Pre-flight | `make test` · `make generate-demo` · `terraform plan` | tests pass · 30 resources to add | ✅ 78 tests · 6,336 history payments + demo day 2026-11-05 · `Plan: 30 to add` |
| 01:16:05 | **Create** | `make deploy` | `30 added` | ✅ in 65 s |
| 01:17 | Master data | `make upload-reference` | 2 reference tables | ✅ |
| ~01:20 | Confirm subscriptions | email links | AP Mexico and AP USA confirmed | ✅ both confirmed |
| 01:23:31 | History | `make upload-history` | 128 files → 128 Lambda runs → 128 validated | ✅ **128 validated, 0 rejected, 0 errors**, but **259 throttles** (section 4) |
| 01:25:33 | **Live demo** | `make demo` | edited file REJECTED + email · correct file VALIDATED · re-sent DUPLICATE · US file VALIDATED | ✅ exactly as expected (section 3) |
| 01:26:56 | **Daily job** | `make kpis` | SUCCEEDED · balanced · Data Quality · duplicate email | ✅ SUCCEEDED in 2 min 44 s · 6,337 rows balanced · **DQ 11/11 rules passed** · 1 duplicate email published |
| 01:31 | Queries | `make queries` | 5 queries on Athena | ✅ 5 queries, results in `docs/images/results/` |
| 01:35 | Email check | 2 test publishes | — | One email was not delivered (section 4) |
| 01:44:46 | **Destroy** | `make destroy` | `30 destroyed` | ✅ in 46 s |
| 01:45 | Glue logs | `make clean-glue-logs` | Glue's own log groups deleted | ✅ 3 deleted |
| 01:45 | Cleanup check | `make leftovers` | `✅ Nothing billable left.` | ✅ clean on all three nets · `terraform state list`: 0 resources |

## 2. Checks after each phase

| Phase | Evidence | What it proves |
|---|---|---|
| Deployed | `docs/evidence/deployed/` | <ul><li>S3 event filter `landing/ap/` + `.csv`</li><li>Lambda: Python 3.12, 256 MB, 30 s, its 5 environment variables</li><li>2 subscriptions filtered by entity</li><li>Glue 5.0, 2 × G.1X, 10 min, trigger **not activated**</li><li>6 Athena tables, 1 GB enforced scan cap</li><li>bucket AES-256 and private</li></ul> |
| Loaded | `docs/evidence/loaded/` | 128 validated files, 0 rejected; Lambda 128 invocations, 0 errors, 259 throttles |
| Demo | `docs/evidence/demo/` | The rejected file, its reason (`TRAILER_TOTAL_MISMATCH`, line 52), and the Lambda's logged decisions |
| Batch | `docs/evidence/batch/` | <ul><li>Glue run SUCCEEDED in 164 s</li><li>its summary line: 6,337 rows, 19 HIGH + 8 MEDIUM suspects, 11/11 Data Quality rules, 1 email sent</li><li>Data Quality results rule by rule (score 1.0)</li><li>the gold files</li></ul> |
| Destroyed | `docs/evidence/destroyed/` | An empty Terraform state and a clean `leftovers` report |

## 3. What happened, step by step

**The live demo** (`make demo`, demo day 2026-11-05):

| Upload | Lambda decision | Detail |
|---|---|---|
| MX01 file with one amount edited by hand | **REJECTED** + email to AP Mexico | `TRAILER_TOTAL_MISMATCH`: trailer 6,794,328.95 vs rows 6,795,328.95, exactly the $1,000 added |
| The correct MX01 file | **VALIDATED** | Copied to `validated/ap/entity=MX01/dt=2026-11-05/` |
| The same correct file again | **DUPLICATE** | Same SHA-256: ignored, no email |
| The US01 file (empty: US pays on Fridays) | **VALIDATED** | A "0 records" file is a valid export |

**The detector, graded on the real Athena output** (`sql/02_duplicate_review_list.sql`) against the answer key:

| Planted problem | Planted | Found | $ at risk |
|---|---|---|---|
| Same invoice, number retyped (12 in history + the demo's) | 13 | **13** | $137,355 |
| Advance, then the full amount | 6 | **6** | $35,058 |
| Resubmitted under a new number | 8 | **8** | $23,004 |
| **Total** | **27** | **27** | **$195,416**, 0 false alarms |

**Payment KPIs** (90 days, $58.5M paid):

| | MX01 | US01 |
|---|---|---|
| On-time payment % | 85.0% (SUP-ANDINA: 0%) | 92.1% |
| DPO | 20.7 days | 30.0 days |
| Discounts lost | $36,226 (94 invoices) | **$361,287** (1,041 invoices) |

The same five SQL files returned the same results on Athena as on local Spark.

## 4. Differences from the plan

1. **259 Lambda throttles while loading the history: the account allows only 10 concurrent executions.** `aws lambda get-account-settings` shows a concurrency limit of **10**; it is usually 1,000 and is lower on new or little-used accounts. Uploading 128 files at once produced 128 S3 events. Only 10 could run at a time, and the rest were throttled 259 times in total.
   - **Nothing was lost.** S3 invokes Lambda **asynchronously**: throttled events wait in Lambda's internal queue and are retried automatically. The Monitor tab shows 132 async events received and 0 dropped; the oldest waited 25.6 s.
   - Final result: 128 of 128 files validated, 0 errors.
   - Lesson 2 in `docs/lessons-learned.md`.
2. **One email was not delivered: the duplicate-payment email of the demo day.**
   - **What worked:** the Glue job logged `messages: 1`. SNS counted 2 published messages, 0 failed deliveries, and 2 filtered out (the AP USA subscription correctly received neither, since both emails were for MX01). The file-rejected email arrived.
   - **What didn't:** the duplicate email reached no folder of the mailbox.
   - **What the test showed:** two test publishes, one with exactly the same subject and body and one with neutral text, **both arrived**. So the content, the filter policy and the permissions were all fine.
   - **Why the cause can't be pinned down:** for email, SNS only confirms the hand-off to the mail provider.
   - Lesson 3.
3. **Glue Data Quality worked on its first run on AWS.** It is the only part of the code that couldn't run locally (ADR-008). All 11 rules passed, with a score of 1.0.

## 5. Actual cost

_From `make cost-daily` once Cost Explorer has the data (it lags up to 24 h)._

## 6. Teardown proof

- `terraform destroy`: `30 destroyed`
- `terraform state list`: **0 resources** (`docs/evidence/destroyed/terraform-state.txt`)
- `make leftovers`: **✅ Nothing billable left.** (`docs/evidence/destroyed/leftovers.txt`): tag, names and hourly services
- Glue's own log groups: deleted with `make clean-glue-logs`
