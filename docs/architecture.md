# Architecture

## 1. The big picture

![AWS architecture diagram drawn with the official AWS icons](images/architecture-aws.png)

<sub>Drawn in Lucidchart with the official AWS 2024 icon set. The Mermaid version below is the same flow as text, kept next to the code so it changes with it.</sub>

Two speeds, one source of files:

| Speed | Question it answers | Path |
|---|---|---|
| **Seconds** | "Did today's ERP file arrive complete and untouched?" | S3 event → Lambda `file_validator` → `validated/` or `rejected/` + email |
| **Daily** | "Did we pay any invoice twice? Are we paying on time, and losing discounts?" | `validated/` → Glue job `daily_ap_kpis` → `curated/` + `gold/` → Athena · email for proven duplicates |

```mermaid
flowchart LR
    subgraph ERP["ERP (simulated: src/generators/)"]
        EXP[Daily export<br/>1 CSV per entity<br/>+ control trailer]
        REF[Master data<br/>suppliers · entities]
    end

    subgraph Lake["S3 data lake (one bucket, one prefix per zone)"]
        LAND[(landing/ap/<br/>as received, never changed)]
        VAL[(validated/ap/<br/>entity= · dt=)]
        REJ[(rejected/ap/<br/>file + .reason.json)]
        RSRC[(reference/)]
        CUR[(curated/payments/)]
        QUA[(quarantine/payments/)]
        GOLD[(gold/<br/>duplicate_suspects · payment_performance<br/>discounts_lost · data_quality)]
    end

    subgraph Check["Seconds: file integrity"]
        LAMBDA[Lambda file_validator<br/>trailer · header · rows · re-sends]
    end

    subgraph Batch["Daily: duplicates and KPIs"]
        GLUE[Glue job daily_ap_kpis<br/>PySpark]
        DQ[Glue Data Quality<br/>dq/curated_payments.dqdl]
    end

    subgraph Serve["Analysts and alerts"]
        CAT[Glue Data Catalog<br/>6 tables, partition projection]
        ATH[Athena workgroup<br/>1 GB scan cap · sql/]
        SNS[[SNS topic<br/>filtered by kind and entity]]
        AP((AP analysts))
    end

    EXP -->|S3 PUT| LAND
    REF --> RSRC
    LAND -->|S3 event| LAMBDA
    LAMBDA -->|valid: copy| VAL
    LAMBDA -->|invalid: copy + reasons| REJ
    LAMBDA -->|FILE_REJECTED| SNS
    VAL --> GLUE
    RSRC --> GLUE
    GLUE --> CUR
    GLUE --> QUA
    GLUE --> GOLD
    GLUE --- DQ
    GLUE -->|DUPLICATE_PAYMENT, HIGH only| SNS
    GOLD --> CAT --> ATH --> AP
    SNS -->|email| AP
```

## 2. Data lineage: where every number comes from

Each zone is produced by one component from the zone before it, so any KPI can be traced back to the exact ERP file and line it came from.

```mermaid
flowchart TB
    A["landing/ap/entity=MX01/MX01_ap_20261105.csv<br/><i>exactly what the ERP sent: rows + control trailer</i>"]
    B["validated/ap/entity=MX01/dt=2026-11-05/MX01_ap_20261105.csv<br/><i>same bytes, + metadata: sha256, record count, total</i>"]
    R["rejected/ap/entity=MX01/MX01_ap_20261105.csv + .reason.json<br/><i>when the file fails a check</i>"]
    C["curated/payments/dt=…<br/><i>one row per real payment: trailer dropped, rows validated, duplicate rows dropped,<br/>typed, converted to USD, supplier category and discount terms joined</i>"]
    Q["quarantine/payments/dt=…<br/><i>rows that broke a business rule, + quarantine_reason</i>"]
    G1["gold/duplicate_suspects<br/><i>KPI 1: one row per suspect payment</i>"]
    G2["gold/payment_performance<br/><i>KPIs 2–3: on-time counts, DPO inputs</i>"]
    G3["gold/discounts_lost<br/><i>KPI 4: one row per invoice that lost its discount</i>"]
    G4["gold/data_quality<br/><i>file rows = curated + duplicate rows + quarantined, per day</i>"]
    S["sql/01…05 on Athena<br/><i>ROLLUP Entity → Category → Supplier · the review list</i>"]

    A -->|"Lambda: integrity checks (validation.py)"| B
    A -->|"Lambda: integrity checks"| R
    B -->|"Glue: drop_trailers → add_columns → validate_rows"| C
    B -->|"Glue: validate_rows"| Q
    C -->|"detect_duplicates (ADR-005, ADR-006)"| G1
    C -->|"build_payment_performance"| G2
    C -->|"build_discounts_lost"| G3
    C -->|"build_data_quality"| G4
    G1 --> S
    G2 --> S
    G3 --> S
    G4 --> S
```

**Tracing one duplicate back to its source:**
1. `gold/duplicate_suspects`: `suspect_payment_id = PAY-MX01-003650`, rule `SAME_INVOICE_RETYPED_NUMBER`.
2. `curated/payments`: the row with that id shows `supplier_invoice_number = "rx 938"`, `invoice_key = RX938`.
3. `validated/ap/entity=MX01/dt=2026-11-05/MX01_ap_20261105.csv`: the line of that payment, exactly as exported.
4. `landing/ap/…`: the identical file as received. Its SHA-256 in the validated copy's metadata proves it is the same.

## 3. One file, then one day, step by step

```mermaid
sequenceDiagram
    autonumber
    participant ERP as ERP export
    participant S3 as S3 landing/
    participant L as Lambda file_validator
    participant V as S3 validated/ · rejected/
    participant N as SNS
    participant G as Glue daily_ap_kpis
    participant A as Athena
    participant P as AP analysts

    ERP->>S3: PUT MX01_ap_20261105.csv
    S3-->>L: event (async: throttled events wait and retry)
    L->>L: key → entity + day · header = contract · every row complete · trailer count and total
    alt same entity and day already validated, same SHA-256
        L-->>L: DUPLICATE: ignore (a retry), no email
    else any check fails, or different bytes for a loaded day
        L->>V: copy to rejected/ + .reason.json
        L->>N: FILE_REJECTED (entity attribute)
    else valid
        L->>V: copy to validated/ with sha256, count, total as metadata
    end
    Note over G: daily (trigger disabled in the demo) or on demand
    G->>V: read every validated file + reference/
    G->>G: validate rows · drop duplicate rows · detect duplicate payments · KPIs
    G->>G: reconcile (fail before writing if any row is missing) · Glue Data Quality
    G->>A: write curated/, quarantine/, gold/ (Parquet, dt partitions)
    G->>N: DUPLICATE_PAYMENT for the newest day's HIGH suspects (entity attribute)
    N-->>P: emails, each person filtered by kind and entity
    P->>A: review list and KPIs (sql/)
```

## 4. Components

| Component | Code | Responsibility |
|---|---|---|
| ERP simulator | `src/generators/` | 90 days of daily files with control trailers, planted problems, the demo day, the answer key |
| File validator (Lambda) | `src/lambdas/file_validator/` | `validation.py` pure checks · `storage.py` S3 zones · `notifier.py` email · `handler.py` decision |
| Daily job (Glue, PySpark) | `src/glue_jobs/` | `transforms.py` pure rules, detection and KPIs · `data_quality.py` DQDL evaluation · `alerts.py` duplicate email · `daily_ap_kpis.py` extract → transform → reconcile → quality → load → alert |
| Data Quality ruleset | `dq/curated_payments.dqdl` | 11 table-level rules, tested against the file contract |
| Queries | `sql/01…05` | ROLLUP KPIs and the review list, portable between Athena and Spark |
| Grading | `src/evaluation/grading.py` | Precision and recall of the detector against the answer key |
| Infrastructure | `infra/terraform/` | `1_ingestion.tf` S3 event + Lambda · `2_batch.tf` Glue + DQ · `3_alerts.tf` SNS · `4_analytics.tf` catalog + workgroup |

## 5. Storage layout

```
s3://ap-duplicate-payment-detection-<account>/
├── landing/ap/entity=<E>/<E>_ap_<YYYYMMDD>.csv          as received (audit trail; never changed)
├── validated/ap/entity=<E>/dt=<YYYY-MM-DD>/<file>       passed every check (+ sha256 metadata)
├── rejected/ap/entity=<E>/<file> + <file>.reason.json   failed a check, with the reasons
├── reference/suppliers/ · reference/entities/            master data
├── curated/payments/dt=<YYYY-MM-DD>/*.parquet            one row per real payment
├── quarantine/payments/dt=<YYYY-MM-DD>/*.parquet         rows that broke a business rule
├── gold/duplicate_suspects/ · payment_performance/ · discounts_lost/ · data_quality/
├── scripts/                                              the Glue script, its modules, the DQDL ruleset
└── athena-results/                                       query results (expire after 7 days)
```

## 6. Security

- **One IAM role per component**, least privilege:
  - the Lambda reads `landing/`, writes `validated/` and `rejected/`, and publishes to one topic;
  - Glue reads `validated/`, `reference/` and `scripts/`, writes `curated/`, `quarantine/` and `gold/` (plus their folder markers), and publishes to the topic and to Data Quality.
- **S3 trigger scoped** to `landing/ap/*.csv` and to this bucket and account (the Lambda's resource policy).
- **Encryption:** S3 at rest (AES-256), HTTPS required by bucket policy, no public access. No customer-managed KMS keys.
- **No personal data in the repo:** email addresses live in the gitignored `config/subscribers.yaml`; evidence is redacted.
