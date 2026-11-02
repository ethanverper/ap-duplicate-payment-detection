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
