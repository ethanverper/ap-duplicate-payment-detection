# Configuration and setup

Everything you can change, where it lives, and what to do after changing it. Someone running the project for the first time should only need this page and the README.

> Built level by level: this page grows with each level. Today it covers the data generator (level 1) and the file validator (level 2).

## 1. Prerequisites
| Tool | Version | Why | Check |
|---|---|---|---|
| Python | ≥ 3.10 | Generator, tests, scripts | `python3 --version` |
| Terraform | ≥ 1.6 (native arm64 on Apple Silicon) | Creates the AWS resources (level 4) | `terraform version` |
| AWS CLI | v2 | Uploads and checks (level 4) | `aws sts get-caller-identity` |

```bash
make install   # Python dependencies
```

## 2. Configuration files at a glance
| File | In git? | What it holds | Who reads it |
|---|---|---|---|
| `config/settings.yaml` | ✅ | Every tunable value: reporting rate, simulation volume and policies, the planted problems, file layout | Python (`src/common/settings.py`); Terraform from level 4 |
| `config/master_data.yaml` | ✅ | Entities, supplier categories, suppliers and their payment terms | Python (`src/generators/reference.py`) |

## 3. What each file contains

### `config/settings.yaml`
| Section / key | Meaning | Change it when… |
|---|---|---|
| `project.name`, `project.region` | Prefix of every AWS resource; AWS region | Deploying under another name or region |
| `reporting.mxn_per_usd` | Fixed rate used to report MXN amounts in USD | You want another rate (all KPIs are in USD) |
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

The file-validator Lambda (`src/lambdas/file_validator/`) reads only environment variables. Terraform will set them in level 4:

| Environment variable | Source |
|---|---|
| `KNOWN_ENTITIES` | `config/master_data.yaml` → `entities` keys, comma-separated (`MX01,US01`) |
| `VALIDATED_PREFIX` | `files.validated_prefix` |
| `REJECTED_PREFIX` | `files.rejected_prefix` |
| `MAX_FILE_MB` | `files.max_file_mb` |
| `ALERT_TOPIC_ARN` | The SNS topic Terraform creates |

If one is missing, the Lambda stops at start-up rather than judging files with a made-up default.

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

## 6. Running it, in order
| Step | Command | What happens | Cost |
|---|---|---|---|
| 1 | `make test` | Unit tests, plus the file validator against simulated S3 and SNS (moto) | $0 |
| 2 | `make generate` | 90 days of ERP files, master data and the answer key in `data/generated/` | $0 |

## 7. Common changes
| I want to… | Edit | Then |
|---|---|---|
| Plant more or fewer duplicates | `config/settings.yaml` → `simulation.incidents` | `make generate`, `make test` (some tests assert the configured counts) |
| Add a supplier | `config/master_data.yaml` → `suppliers` | `make generate`, `make test` |
| Change an entity's payment calendar | `config/settings.yaml` → `simulation.payment_weekdays` | `make generate` |
