"""All S3 access of the file validator: read the new file, find an earlier copy, accept or reject it.

The lake's file zones (prefixes from config/settings.yaml -> files, passed as environment variables):

    landing/ap/entity=<E>/<file>                   as received from the ERP; never changed or deleted
    validated/ap/entity=<E>/dt=<YYYY-MM-DD>/<file>  a copy, once it passed every check (what Glue reads)
    rejected/ap/entity=<E>/<file>                   a copy of a file that failed...
    rejected/ap/entity=<E>/<file>.reason.json       ...and why, for the AP analyst who has to fix it

Why copy instead of move: the landing zone stays an exact record of what the ERP sent (audit trail),
and a validated copy can always be rebuilt from it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.common import config
from src.lambdas.file_validator.validation import ContentCheck, FileIdentity, Issue

# Environment variable names (set by Terraform from config/settings.yaml)
ENV_VALIDATED_PREFIX = "VALIDATED_PREFIX"
ENV_REJECTED_PREFIX = "REJECTED_PREFIX"

# S3 object metadata keys written on every validated copy (read back to detect re-sent files)
META_SHA256 = "sha256"
META_RECORD_COUNT = "record-count"
META_TOTAL_PAID = "total-paid"
META_SOURCE_KEY = "source-key"

# Folder used for rejected files whose key didn't even say which entity they belong to
UNKNOWN_ENTITY_FOLDER = "unknown"


@dataclass(frozen=True)
class Prefixes:
    """Where validated and rejected files go."""

    validated: str
    rejected: str

    @classmethod
    def from_env(cls) -> Prefixes:
        """Read both prefixes from the Lambda's environment (required)."""
        return cls(validated=config.get_str(ENV_VALIDATED_PREFIX), rejected=config.get_str(ENV_REJECTED_PREFIX))


class FileStore:
    """Reads and writes the AP files in one bucket."""

    def __init__(self, client: Any, bucket: str, prefixes: Prefixes) -> None:
        """Wrap an S3 client (tests pass one backed by moto)."""
        self.client = client
        self.bucket = bucket
        self.prefixes = prefixes

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def size(self, key: str) -> int:
        """Object size in bytes, without downloading it (to refuse huge files cheaply)."""
        return self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]

    def read(self, key: str) -> bytes:
        """The object's exact bytes."""
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def validated_fingerprint(self, identity: FileIdentity) -> str | None:
        """SHA-256 of the copy already validated for this entity and day, or None if there is none."""
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self.validated_key(identity))
        except self.client.exceptions.ClientError as error:
            # 404 = no earlier copy; anything else (permissions, throttling) must surface
            if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return head["Metadata"].get(META_SHA256)

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    def validated_key(self, identity: FileIdentity) -> str:
        """Where the validated copy of a file lives: partitioned by entity and day for Glue and Athena."""
        partition = f"entity={identity.entity}/dt={identity.file_date.isoformat()}"
        return f"{self.prefixes.validated}/{partition}/{identity.file_name}"

    def rejected_key(self, source_key: str, identity: FileIdentity | None) -> str:
        """Where a rejected file is quarantined (by entity, if the key said which one)."""
        folder = identity.entity if identity else UNKNOWN_ENTITY_FOLDER
        return f"{self.prefixes.rejected}/entity={folder}/{source_key.rsplit('/', 1)[-1]}"

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def accept(self, source_key: str, identity: FileIdentity, check: ContentCheck) -> str:
        """Copy a valid file to the validated zone, with its fingerprint and totals as metadata.

        Returns:
            The validated key.
        """
        target = self.validated_key(identity)
        self.client.copy_object(
            Bucket=self.bucket,
            Key=target,
            CopySource={"Bucket": self.bucket, "Key": source_key},
            MetadataDirective="REPLACE",  # write our metadata instead of copying the original's
            Metadata={
                META_SHA256: check.sha256,
                META_RECORD_COUNT: str(check.record_count),
                META_TOTAL_PAID: check.total_paid,
                META_SOURCE_KEY: source_key,
            },
        )
        return target

    def reject(self, source_key: str, identity: FileIdentity | None, issues: list[Issue]) -> str:
        """Quarantine a file: copy it to the rejected zone and write the reasons next to it.

        Returns:
            The rejected key.
        """
        target = self.rejected_key(source_key, identity)
        self.client.copy_object(Bucket=self.bucket, Key=target, CopySource={"Bucket": self.bucket, "Key": source_key})

        # The reasons, as JSON, for the analyst (and for anyone auditing what was refused)
        reasons = {
            "source_key": source_key,
            "issues": [{"code": i.code, "message": i.message, "line": i.line} for i in issues],
        }
        self.client.put_object(
            Bucket=self.bucket,
            Key=f"{target}.reason.json",
            Body=json.dumps(reasons, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return target
