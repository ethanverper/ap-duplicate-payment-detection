"""Lambda entry point: S3 says a file landed; decide VALIDATED, DUPLICATE or REJECTED for it.

Where it sits in the pipeline:
    ERP → s3://<bucket>/landing/ap/... ─(S3 event notification)→ this Lambda
          ├─ VALIDATED  copy to validated/ (Glue reads only from there)
          ├─ DUPLICATE  the exact same file was already validated: nothing to do (idempotent)
          └─ REJECTED   copy to rejected/ + reasons + email to AP

The order of the decisions, for one file:
    1. The key must say a known entity and a real day.
    2. The size must be sane (a huge object is refused without downloading it).
    3. If this entity and day were already validated:
         same bytes (same SHA-256)   -> DUPLICATE: a re-sent file, ignored, no email;
         different bytes             -> REJECTED: a silent overwrite of loaded data is never allowed.
    4. The content must pass every integrity check (validation.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any
from urllib.parse import unquote_plus

from src.common import config
from src.common.aws import get_client
from src.common.logging_utils import get_logger
from src.lambdas.file_validator.notifier import Notifier
from src.lambdas.file_validator.storage import FileStore, Prefixes
from src.lambdas.file_validator.validation import (
    TOO_LARGE,
    FileIdentity,
    Issue,
    check_content,
    check_key,
)

logger = get_logger(__name__)

# Outcomes, one per file
VALIDATED = "VALIDATED"
DUPLICATE = "DUPLICATE"
REJECTED = "REJECTED"

# Issue code for a different file arriving for an entity and day that were already loaded
ALREADY_LOADED = "ALREADY_LOADED_DIFFERENT_CONTENT"

# Environment variables (set by Terraform from config/settings.yaml)
ENV_KNOWN_ENTITIES = "KNOWN_ENTITIES"  # e.g. "MX01,US01"
ENV_MAX_FILE_MB = "MAX_FILE_MB"
BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    """What the handler needs to decide, read once from the environment."""

    known_entities: frozenset[str]
    max_bytes: int
    prefixes: Prefixes
    topic_arn: str

    @classmethod
    def from_env(cls) -> Settings:
        """Read every setting; a missing one stops the Lambda at start-up instead of misjudging files."""
        return cls(
            known_entities=frozenset(config.get_str(ENV_KNOWN_ENTITIES).split(",")),
            max_bytes=int(config.get_float(ENV_MAX_FILE_MB, 50) * BYTES_PER_MB),
            prefixes=Prefixes.from_env(),
            topic_arn=Notifier.topic_from_env(),
        )


@cache
def _settings() -> Settings:
    """Settings, read once per Lambda container (reused across invocations)."""
    return Settings.from_env()


def process_file(store: FileStore, notifier: Notifier, settings: Settings, key: str) -> dict[str, Any]:
    """Decide and act on one landed file.

    Args:
        store: S3 access for the bucket the file landed in.
        notifier: Sends the rejection email.
        settings: Known entities, size limit, prefixes.
        key: The landed object's key.

    Returns:
        A small summary for the logs: outcome, key, and where the file went.
    """
    # 1. The key must name a known entity and a real day
    identity, issues = check_key(key, set(settings.known_entities))
    if issues:
        return _reject(store, notifier, key, identity, issues)

    # 2. Refuse absurdly large objects before downloading them
    size = store.size(key)
    if size > settings.max_bytes:
        return _reject(store, notifier, key, identity, [Issue(TOO_LARGE, f"{size} bytes, limit {settings.max_bytes}")])

    # 3-4. Read it, fingerprint it, check it
    check = check_content(store.read(key), identity)
    previous = store.validated_fingerprint(identity)
    if previous == check.sha256:
        logger.info("Duplicate file ignored", extra={"fields": {"key": key, "sha256": check.sha256}})
        return {"outcome": DUPLICATE, "key": key}
    if previous is not None:
        already = Issue(ALREADY_LOADED, f"{identity.entity} {identity.file_date} was already loaded with other content")
        return _reject(store, notifier, key, identity, [already, *check.issues])
    if not check.is_valid:
        return _reject(store, notifier, key, identity, check.issues)

    # Valid: copy to the validated zone with its fingerprint and totals
    target = store.accept(key, identity, check)
    logger.info(
        "File validated",
        extra={"fields": {"key": key, "target": target, "records": check.record_count, "total_paid": check.total_paid}},
    )
    return {"outcome": VALIDATED, "key": key, "target": target, "records": check.record_count}


def _reject(store: FileStore, notifier: Notifier, key: str, identity: FileIdentity | None, issues: list[Issue]) -> dict:
    """Quarantine a file, email AP, and log why."""
    target = store.reject(key, identity, issues)
    message_id = notifier.file_rejected(key, identity, issues)
    logger.warning(
        "File rejected",
        extra={"fields": {"key": key, "target": target, "issues": [i.code for i in issues], "message_id": message_id}},
    )
    return {"outcome": REJECTED, "key": key, "target": target, "issues": [i.code for i in issues]}


def handler(event: dict, context: Any = None) -> dict:
    """Lambda entry point for S3 "object created" notifications.

    Args:
        event: The S3 event (one or more records).
        context: Lambda context (unused).

    Returns:
        One summary per file. An unexpected error (S3 throttling, permissions) is raised, so Lambda
        retries the event; every step is safe to repeat (copies overwrite the same keys).
    """
    settings = _settings()
    s3 = get_client("s3")
    notifier = Notifier(get_client("sns"), settings.topic_arn)

    results = []
    for record in event.get("Records", []):
        # S3 URL-encodes keys in events (a space arrives as "+")
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        results.append(process_file(FileStore(s3, bucket, settings.prefixes), notifier, settings, key))
    return {"files": results}
