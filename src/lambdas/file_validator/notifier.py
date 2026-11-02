"""The rejection email: tells AP which file was refused, why, and what to do (SNS)."""

from __future__ import annotations

from typing import Any

from src.common import config
from src.lambdas.file_validator.validation import FileIdentity, Issue

# Environment variable with the topic ARN (set by Terraform)
ENV_ALERT_TOPIC_ARN = "ALERT_TOPIC_ARN"

# Message attribute values: subscribers filter on them (an analyst may only want one entity)
KIND_FILE_REJECTED = "FILE_REJECTED"

# SNS subjects must be at most 100 characters
MAX_SUBJECT = 100


def rejection_message(source_key: str, identity: FileIdentity | None, issues: list[Issue]) -> tuple[str, str]:
    """Build the email subject and body for a rejected file (pure, so it is unit-tested).

    Args:
        source_key: The file as received.
        identity: Entity and day, if the key could be read.
        issues: Why it was rejected.

    Returns:
        (subject, body).
    """
    # Subject: which entity and file, short enough for SNS
    label = f"{identity.entity} {identity.file_date}" if identity else "unknown entity"
    subject = f"[AP] File rejected: {label} ({len(issues)} issue{'s' if len(issues) != 1 else ''})"[:MAX_SUBJECT]

    # Body: every issue with its line, then the action to take
    lines = [f"The AP payment file {source_key} was NOT loaded.", "", "Issues:"]
    for issue in issues:
        where = f"line {issue.line}: " if issue.line else ""
        lines.append(f"  - {issue.code}: {where}{issue.message}")
    lines += [
        "",
        "Action: ask the ERP team to re-export this file.",
        "Nothing from it reached the KPIs, so nothing has to be undone.",
    ]
    return subject, "\n".join(lines)


class Notifier:
    """Publishes rejection alerts to the AP topic."""

    def __init__(self, client: Any, topic_arn: str) -> None:
        """Wrap an SNS client and the topic ARN."""
        self.client = client
        self.topic_arn = topic_arn

    @classmethod
    def topic_from_env(cls) -> str:
        """The topic ARN from the environment (required)."""
        return config.get_str(ENV_ALERT_TOPIC_ARN)

    def file_rejected(self, source_key: str, identity: FileIdentity | None, issues: list[Issue]) -> str:
        """Send the rejection email. Returns the SNS message id."""
        subject, body = rejection_message(source_key, identity, issues)
        response = self.client.publish(
            TopicArn=self.topic_arn,
            Subject=subject,
            Message=body,
            # Attributes let each subscription filter what it receives (same idea as P1)
            MessageAttributes={
                "kind": {"DataType": "String", "StringValue": KIND_FILE_REJECTED},
                "entity": {"DataType": "String", "StringValue": identity.entity if identity else "UNKNOWN"},
            },
        )
        return response["MessageId"]
