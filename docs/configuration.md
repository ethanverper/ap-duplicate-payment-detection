# Configuration and setup

Everything you can change, where it lives, and what to do after changing it. Someone running the project for the first time should only need this page and the README.

> Built level by level: this page grows with each level. Today it covers the data generator (level 1), the file validator (level 2), the duplicate detector and KPI job (level 3), and the AWS infrastructure (level 4).

## 1. Prerequisites
| Tool | Version | Why | Check |
|---|---|---|---|
| Python | ≥ 3.10 | Generator, tests, scripts | `python3 --version` |
| Java | 17 (Spark 3.5's supported version) | Local Spark for the Glue job tests and `make kpis-local`. Without it those tests are skipped. | `java -version` |
| Terraform | ≥ 1.6 (native arm64 on Apple Silicon) | Creates the AWS resources (level 4) | `terraform version` |
| AWS CLI | v2 | Uploads and checks (level 4) | `aws sts get-caller-identity` |

```bash
make install   # Python dependencies (boto3, pyyaml, pytest, ruff, moto, pyspark 3.5 ≈ 300 MB)
```

## 2. Configuration files at a glance
| File | In git? | What it holds | Who reads it |
|---|---|---|---|
| `config/settings.yaml` | ✅ | Every tunable value: reporting rate, simulation volume and policies, the planted problems, file layout | Python (`src/common/settings.py`); Terraform from level 4 |
| `config/master_data.yaml` | ✅ | Entities, supplier categories, suppliers and their payment terms | Python (`src/generators/reference.py`); Terraform (entity list for the Lambda) |
| `config/subscribers.yaml` | ❌ gitignored | Who receives which AP emails (real addresses) | Terraform (SNS subscriptions) |
| `config/subscribers.example.yaml` | ✅ | Template of the file above | You |
| `dq/curated_payments.dqdl` | ✅ | Glue Data Quality ruleset for the curated payments | The Glue job (uploaded by Terraform) |

## 3. What each file contains

### `config/settings.yaml`
| Section / key | Meaning | Change it when… |
|---|---|---|
| `project.name`, `project.region` | Prefix of every AWS resource; AWS region | Deploying under another name or region |
| `reporting.mxn_per_usd` | Fixed rate used to report MXN amounts in USD | You want another rate (all KPIs are in USD) |
| `detection.overpayment_tolerance` | Excess paid on one invoice below this (invoice currency) is rounding, not an overpayment | Rounding differences in your ERP are larger |
| `detection.resubmission_window_days` | Max days between two same-amount invoices of a supplier to call the later one a resubmission (MEDIUM). Wider catches later resubmissions but adds false alarms (lesson 1). | Suppliers resend invoices later |
| `kpis.on_time_alert_pct` | On-time % below which a supplier is highlighted | AP's target changes |
| `simulation.seed` | Random seed: same seed, same files, same README numbers | You want a different but reproducible history |
| `simulation.history_days`, `warm_up_days` | Days of files produced; how far back invoices start, so day 1 already has payments due | More or less history |
| `simulation.invoices_per_day.<ENTITY>` | New invoices per business day | More or less volume |
| `simulation.payment_weekdays.<ENTITY>` | Weekdays the entity runs payments (0 = Monday) | Changing an entity's payment calendar |
| `simulation.take_discounts.<ENTITY>` | `true`: invoices with discount terms are paid by the discount deadline; `false`: by the due date | Testing the discounts-lost KPI |
| `simulation.advance_share`, `advance_pct`, `advance_after_days` | How many service invoices are split into advance + final, how big the advance is, when it is paid | Changing the share of legitimate split payments |
| `simulation.incidents.format_duplicate` | Problem 1: entity, count, days between the payments | Planting more or fewer number-format duplicates |
| `simulation.incidents.resubmitted_invoice` | Problem 2: entity, supplier, count, days between invoice dates | Planting resubmissions somewhere else |
| `simulation.incidents.advance_overpaid` | Problem 3: count (split between entities) | More or fewer overpaid advances |
| `simulation.incidents.late_supplier` | Problem 5: entity, supplier, days late | Another chronically late supplier |
| `files.landing_prefix` | Where the ERP files land, as received (and the local output folder layout) | Changing the lake layout |
| `files.validated_prefix` | Where valid files are copied: `<prefix>/entity=<E>/dt=<YYYY-MM-DD>/<file>` | Changing the lake layout |
| `files.rejected_prefix` | Where refused files are copied, each with a `.reason.json` | Changing the lake layout |
| `files.max_file_mb` | Files above this size are refused without being read | Exports get much bigger |
| `aws.lambda` | `timeout_seconds`, `memory_mb`, `log_retention_days` of the file validator | Bigger files |
| `aws.glue` | `glue_version`, `worker_type`, `number_of_workers`, `timeout_minutes`, `schedule_cron`, `schedule_enabled` | More data; turning the daily schedule on (it bills every day) |
| `aws.athena` | `bytes_scanned_cutoff_per_query`, `results_expiration_days`, `projection_days_back` (must cover the history: 120 for 90 days) | Querying older data |

### `config/subscribers.yaml` (who receives the AP emails)
Copy `config/subscribers.example.yaml` to `config/subscribers.yaml` and put real addresses there. Terraform creates one SNS email subscription per entry, with a filter policy built from its optional lists.

| Field | Required | Meaning | Example |
|---|---|---|---|
| `email` | ✅ | Where the emails go | `ap.mexico@example.com` |
| `role` | ✅ | Who it is (documentation, and part of the subscription's name) | `AP Mexico` |
| `alerts` | optional | Kinds to receive: `FILE_REJECTED` (from the Lambda), `DUPLICATE_PAYMENT` (from the Glue job). Omit = all. | `[FILE_REJECTED]` |
| `entities` | optional | Entities to receive: `MX01`, `US01`. Omit = all. | `[MX01]` |

After `make deploy`, **each address receives "AWS Notification - Subscription Confirmation"; nothing arrives until its link is clicked.**

### `config/master_data.yaml`
- **`entities`:** id → `name`, `currency` (MXN or USD). The first level of every KPI.
- **`categories`:** id → description. The second level.
- **`suppliers`:** id → fields:

  | Field | Required | Meaning | Example |
  |---|---|---|---|
  | `name` | ✅ | Display name | `Precisa Controls (controllers, sensors)` |
  | `category` | ✅ | One of `categories` | `DIRECT_MATERIALS` |
  | `entities` | ✅ | Entities it bills | `[MX01, US01]` |
  | `terms` | ✅ | `NET <days>` or `<pct>/<days> NET <days>` | `2/10 NET 30` |
  | `amount_usd` | ✅ | `[min, max]` invoice amount in USD; MXN invoices are converted | `[6000, 40000]` |
  | `weight` | ✅ | How often it invoices relative to the others (0 = only recurring) | `6` |
  | `invoice_format` | ✅ | How it numbers invoices; `{n}` is its running number | `"FAC-{n:04d}"` |
  | `recurring_fee` | optional | Fixed monthly amount in USD billed on the first business day | `4500` |
  | `advances` | optional | `true` if its invoices can be settled as advance + final | `true` |

## 4. Values passed to AWS components

The file-validator Lambda (`src/lambdas/file_validator/`) reads only environment variables. Terraform sets them in `infra/terraform/1_ingestion.tf`:

| Environment variable | Source |
|---|---|
| `KNOWN_ENTITIES` | `config/master_data.yaml` → `entities` keys, comma-separated (`MX01,US01`) |
| `VALIDATED_PREFIX` | `files.validated_prefix` |
| `REJECTED_PREFIX` | `files.rejected_prefix` |
| `MAX_FILE_MB` | `files.max_file_mb` |
| `ALERT_TOPIC_ARN` | The SNS topic Terraform creates |

If one is missing, the Lambda stops at start-up rather than judging files with a made-up default.

The Glue job (`src/glue_jobs/daily_ap_kpis.py`) reads only its job arguments. Terraform sets them in `infra/terraform/2_batch.tf` (`default_arguments`); locally, `scripts/kpis_local.py` builds the same list:

| Job argument | Source |
|---|---|
| `--input_root`, `--output_root` | `s3://<bucket>` on AWS; `data/generated` and `data/lake-local` locally |
| `--files_prefix` | `files.validated_prefix` on AWS (the Lambda's output); `files.landing_prefix` locally (no Lambda in between) |
| `--overpayment_tolerance` | `detection.overpayment_tolerance` |
| `--resubmission_window_days` | `detection.resubmission_window_days` |
| `--dq_ruleset_path` | `s3://<bucket>/scripts/dq/curated_payments.dqdl` on AWS; `dq/curated_payments.dqdl` locally (read and checked, evaluated only on Glue) |
| `--alert_topic_arn` | The SNS topic on AWS; empty locally (no email) |
| `--alert_date` | Optional: the payment date whose HIGH suspects are emailed (default: the newest date in the data) |

**Infrastructure** (`infra/terraform/`, 30 resources): the bucket (private, encrypted, TLS only), the S3 → Lambda trigger on `landing/ap/*.csv`, the Lambda, the Glue job with its trigger **disabled**, the SNS topic and subscriptions, 6 Athena tables with partition projection, and a workgroup capped at 1 GB per query. `terraform output` gives `bucket`, `file_validator_function`, `glue_job_name`, `alerts_topic_arn`, `alert_subscriptions`, `athena_workgroup` and `athena_database`.

**What the Lambda decides for each file** (`docs/decisions.md`, ADR-002 to ADR-004):

| Outcome | When | What happens |
|---|---|---|
| `VALIDATED` | Name, header, every row and the control trailer check out | Copied to `validated/` with its SHA-256, record count and total as metadata |
| `DUPLICATE` | That entity and day were already validated **with identical bytes** | Nothing (a harmless retry), logged |
| `REJECTED` | Any integrity issue, **or** a different file for a day already loaded | Copied to `rejected/` + `.reason.json`, and an email to AP (SNS, attributes `kind=FILE_REJECTED`, `entity`) |

## 5. Private and generated files (never committed)
| Path | What | Recreate with |
|---|---|---|
| `data/generated/landing/ap/entity=<E>/` | One ERP file per entity per business day (rows + control trailer) | `make generate` |
| `data/generated/reference/` | `suppliers/suppliers.csv`, `entities/entities.csv` | `make generate` |
| `data/generated/truth/planted_problems.csv` | The answer key: which payments are planted problems. Local only, never uploaded: the pipeline must find them on its own. | `make generate` |
| `data/lake-local/` | The Glue job's output run locally: `curated/`, `quarantine/`, `gold/` | `make kpis-local` |
| `config/subscribers.yaml` | Real email addresses | `cp config/subscribers.example.yaml config/subscribers.yaml` |
| `infra/terraform/.terraform/`, `*.tfstate*` | Terraform's provider cache and record of what it created. **Keep the state until `make destroy`.** | `terraform init` / `apply` |
| `infra/terraform/build/` | The Lambda and Glue zips, and `deploy_started_at` (used by `make clean-glue-logs`) | `make deploy` |

## 6. Running it, in order
| Step | Command | What happens | Cost |
|---|---|---|---|
| 1 | `make test` | Unit tests, the file validator against simulated S3 and SNS (moto), and the Glue job on local Spark (~5 min) | $0 |
| 2 | `make generate` | 90 days of ERP files, master data and the answer key in `data/generated/` | $0 |
| 3 | `make kpis-local` | The Glue job on those files with a local Spark: duplicate suspects, payment KPIs, reconciliation | $0 |
| 4 | `make grade` | Precision and recall of the duplicate detector against the answer key | $0 |
| 5 | `make deploy` | Creates the 30 resources. **Confirm the subscription emails.** | $0 until something runs |
| 6 | `make upload-reference` | Master data to `reference/` | ~$0 |
| 7 | `make upload-history` | 130 ERP files to `landing/`: each triggers the Lambda and lands in `validated/` | ~$0 |
| 8 | `make kpis` | The Glue job on AWS: suspects, KPIs, Data Quality, duplicate emails (~3 min) | ~$0.05 per run |
| 9 | `make destroy` → `make clean-glue-logs` → `make leftovers` | Delete everything, then prove nothing is left | — |

## 7. Common changes
| I want to… | Edit | Then |
|---|---|---|
| Plant more or fewer duplicates | `config/settings.yaml` → `simulation.incidents` | `make generate`, `make test` (some tests assert the configured counts) |
| Add a supplier | `config/master_data.yaml` → `suppliers` | `make generate`, `make test` |
| Change an entity's payment calendar | `config/settings.yaml` → `simulation.payment_weekdays` | `make generate` |
| Catch resubmissions sent later | `config/settings.yaml` → `detection.resubmission_window_days` | `make kpis-local`, `make grade` (watch the false alarms) |
