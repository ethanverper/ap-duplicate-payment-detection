# Design decisions

One short entry per non-obvious choice.

## ADR-001: Files, not a stream; validated the moment they land
- **Context:** an ERP hands AP data over as a daily export file, and banks do the same. Nothing in AP needs sub-second latency, but a broken file must be caught before anyone builds on it.
- **Decision:** an S3 "object created" event triggers a Lambda that checks each file within seconds. Only files that pass are copied to `validated/`, where the daily Glue job reads.
- **Alternatives:**
  - Kinesis: the wrong shape for a daily file, and billed by the hour.
  - A scheduled job that scans `landing/`: a bad file would wait until the next run before anyone hears about it.
- **Consequences:** nothing in the design bills by the hour. A bad file reaches AP's inbox in seconds, not the next morning.

## ADR-002: The Lambda checks the FILE; Glue Data Quality checks the ROWS
- **Context:** some problems break a whole file; others affect one payment.
  - Whole file: cut in transit, a changed export, a trailer that doesn't match.
  - One payment: an unknown supplier, a negative amount.
- **Decision:**
  - The Lambda rejects a file only for integrity problems: name and folder agree, the header is the contract, every row is complete and readable, rows belong to the file's entity and day, and the control trailer matches.
  - Business rules on single rows go to Glue Data Quality (level 3), which quarantines the row and keeps the rest.
- **Alternatives:** all rules in the Lambda. Then one bad row would block a whole day of payments, and the rules would live in code instead of a declared ruleset.
- **Consequences:** a rejected file means "re-export it". A quarantined row means "fix this payment". Two different actions for two different people.

## ADR-003: The landing zone is never changed; files are copied, not moved
- **Context:** finance needs an audit trail of exactly what the ERP sent.
- **Decision:** `landing/` keeps every file as received. A valid file is **copied** to `validated/ap/entity=<E>/dt=<day>/`, partitioned for Glue and Athena. A refused one is copied to `rejected/` with a `.reason.json` next to it.
- **Alternatives:** moving files out of `landing/`. That loses the record of what arrived and when.
- **Consequences:** a few KB of extra storage. `validated/` can always be rebuilt from `landing/`.

## ADR-004: Re-sent files: same bytes are ignored, different bytes are refused
- **Context:** ERPs and SFTP jobs retry uploads, and sometimes someone re-exports a day after "fixing" it.
- **Decision:** each validated copy stores the SHA-256 of its bytes as S3 metadata.
  - A new file for an entity and day already validated, **with the same fingerprint**, is a harmless retry: ignored, no email.
  - **With a different fingerprint** it is refused (`ALREADY_LOADED_DIFFERENT_CONTENT`) and escalated. Data that may already feed the KPIs is never replaced silently.
- **Alternatives:**
  - Last file wins: silent overwrites of financial data.
  - A DynamoDB ledger of fingerprints: it would also catch the same content uploaded under another name, but it adds a table for a case this export can't produce.
- **Consequences:** idempotent with no extra service. A legitimate correction is a deliberate step: remove the validated copy, then re-send.

## ADR-005: One arithmetic rule for "same invoice paid too much" (HIGH confidence)
- **Context:** two planted problems look different on the surface:
  - the same invoice keyed twice with its number typed differently;
  - an advance followed by the full amount.

  Legitimate advance + final payments look almost the same as the second one.
- **Decision:** normalize the invoice number (uppercase, no separators, no leading zeros), group the payments by entity, supplier and normalized number, and flag the invoice when **total paid > invoice amount** (by more than `detection.overpayment_tolerance`). The rule name says which pattern it was: retyped number, advance then full, or simply paid twice.
- **Alternatives:** one hand-written rule per pattern. That means more code, and it is easy to break the legitimate advance case.
- **Consequences:**
  - a legitimate advance + final adds up exactly to the invoice and can never be flagged;
  - the amount at risk is the actual excess paid, not a guess;
  - HIGH confidence: the arithmetic proves money was overpaid.

## ADR-006: "Same amount, new number" is MEDIUM confidence, within 7 days
- **Context:** a resubmitted invoice has a new number, so nothing in the number gives it away. What gives it away is the same supplier, the same exact amount and close invoice dates. But monthly fees repeat an amount by design, and with hundreds of invoices two of them can match to the cent by chance.
- **Decision:** pair invoices of the same entity and supplier with exactly the same amount, dated at most `detection.resubmission_window_days` (7) apart. Flag the later one as **MEDIUM**: an analyst looks before claiming the money.
- **Evidence:** with a 31-day window the same rule also flags subscriptions and a chance match between two freight invoices. With 7 days it flags only the planted resubmissions (lesson 1).
- **Consequences:** a resubmission sent more than 7 days later is missed. Widening the window trades missed duplicates for false alarms, and the setting makes that a business decision.

## ADR-007: The detector is graded against an answer key it never sees
- **Context:** "26 suspects" says nothing about whether they are the right 26.
- **Decision:** the simulator writes `truth/planted_problems.csv` locally and never uploads it. `src/evaluation/grading.py` compares the suspects with it and reports:
  - per kind of problem: recall, with the expected rule and confidence;
  - overall: precision, plus the false alarms themselves.

  The tests assert it.
- **Consequences:**
  - a change to a rule that adds false alarms or loses duplicates fails the build;
  - the README reports precision and recall, not just a count.

## ADR-008: Glue Data Quality reports; it doesn't block the day
- **Context:** bad payments are already quarantined one by one (`validate_rows`). A DQDL ruleset adds table-level assertions: no `payment_id` twice, every value in its domain, enough rows.
- **Decision:**
  - The ruleset lives in the repo (`dq/curated_payments.dqdl`), versioned like code. Its value lists are tested against the file contract and master data.
  - The job evaluates it on the curated table before writing. Results go to the job's **Data quality** tab and the logs: a failed rule is logged as a warning.
  - It does **not** stop the job.
- **Alternatives:** failing the run on any failed rule. A drift nobody planned for, such as a new entity in master data, would then also stop the day's duplicate emails, which are the reason the pipeline exists.
- **Consequences:**
  - The integrity checks that must block are elsewhere: the Lambda (file) and the reconciliation (rows).
  - Glue-only libraries can't run locally, so the evaluation itself is first exercised on AWS (level 5). The ruleset's content is tested locally.

## ADR-009: Duplicate emails come from the Glue job, HIGH only, one per entity
- **Context:** HIGH-confidence suspects are arithmetic proof of an overpayment, and AP should know the same day. MEDIUM suspects need a person's judgment.
- **Decision:**
  - After writing, the job publishes one SNS email per entity, listing that entity's HIGH suspects paid on the newest date.
  - It uses the same topic and message attributes (`kind=DUPLICATE_PAYMENT`, `entity`) as the rejected-file email, so each person's filter policy decides what reaches them.
  - MEDIUM suspects go to the Athena review list (`sql/02_duplicate_review_list.sql`).
- **Alternatives:**
  - A separate Lambda reading the gold table: one more component for the same result.
  - Emailing every suspect: alert fatigue, the lesson of manufacturing-scrap-and-stockout-alerts.
- **Consequences:** a full-refresh run re-finds old suspects, but only the newest day's are emailed, so nobody is alerted twice for the same payment.
