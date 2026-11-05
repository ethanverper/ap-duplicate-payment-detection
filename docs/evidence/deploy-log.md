# Deploy log

The real deploy of this project to AWS: what was created, what was checked at each step, what happened, what it cost, and proof that everything was deleted afterwards.

- **Date:** _YYYY-MM-DD_ · **Region:** us-east-1 · **Window:** _start_ → _end_ UTC (_N_ minutes)
- **Estimated cost:** ≈ $0.10 (`docs/spec.md`, section 5). Nothing in this design bills by the hour. · **Actual cost:** _section 5_
- **Evidence:** CLI output in `docs/evidence/<phase>/` (account id and emails redacted), console screenshots in `docs/images/console/`, Athena results in `docs/images/results/`

## 1. Timeline

| Time (UTC) | Step | Command | Expected | Result |
|---|---|---|---|---|
| | Pre-flight | `make test` · `make generate-demo` · `terraform plan` | tests pass · 30 resources to add | |
| | Create | `make deploy` | `30 added` | |
| | Confirm subscriptions | email links | AP Mexico and AP USA confirmed | |
| | Master data | `make upload-reference` | `reference/suppliers`, `reference/entities` | |
| | History | `make upload-history` | 128 files → 128 Lambda runs → 128 validated, 0 rejected | |
| | Live demo | `make demo` | edited file REJECTED + email · correct file VALIDATED · re-sent DUPLICATE · US file VALIDATED | |
| | Daily job | `make kpis` | SUCCEEDED · all days balanced · Data Quality results · duplicate email to AP Mexico | |
| | Queries | `make queries` | 5 queries, results in `docs/images/results/` | |
| | Destroy | `make destroy` | `30 destroyed` | |
| | Glue logs | `make clean-glue-logs` | Glue's own log groups deleted | |
| | Cleanup check | `make leftovers` | `✅ Nothing billable left.` | |

## 2. Checks after each phase

| Phase | Evidence | What it proves |
|---|---|---|
| Deployed | `docs/evidence/deployed/` | The S3 event filter (`landing/ap/`, `.csv`), the Lambda's settings, 2 filtered subscriptions, Glue 5.0 with 2 workers and the trigger **not activated**, 6 Athena tables, the 1 GB scan cap, the bucket encrypted and private |
| Loaded | `docs/evidence/loaded/` | Every history file validated; Lambda invocations, errors, throttles |
| Demo | `docs/evidence/demo/` | The rejected file, its reasons, and the Lambda's logged decisions |
| Batch | `docs/evidence/batch/` | The Glue run, its summary line (reconciliation, suspects, emails sent), the Data Quality results rule by rule, the gold files |
| Destroyed | `docs/evidence/destroyed/` | An empty Terraform state and a clean `leftovers` report |

## 3. What the data showed (Athena, real run)

_From `docs/images/results/*.csv`._

## 4. Differences from the plan

_Anything that behaved differently from the local tests (Glue Data Quality runs here for the first time), and what was changed._

## 5. Actual cost

_From `make cost-daily` the day after._

## 6. Teardown proof

- `terraform state list`: _0 resources_
- `make leftovers`: _✅ Nothing billable left._
- Cost Explorer two days later: _no charges after the deploy day_
