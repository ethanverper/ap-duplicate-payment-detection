"""The duplicate-payment email: HIGH-confidence suspects of the day, sent to AP through SNS.

Where it sits in the pipeline:
    After the Glue job writes the gold tables, it calls `send_duplicate_alerts()` for the newest
    payment date. HIGH confidence means arithmetic proof of an overpayment (docs/decisions.md,
    ADR-005), so AP is told the same day. MEDIUM suspects only go to the review list (Athena).

One email per entity, with SNS message attributes (kind, entity), so each subscriber's filter
policy decides who receives what: the same mechanism as the rejected-file email of the Lambda.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.common.logging_utils import get_logger

logger = get_logger(__name__)

KIND_DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
HIGH = "HIGH"
MAX_SUBJECT = 100  # SNS subject limit


def duplicate_alert(entity: str, day: str, suspects: list[dict]) -> tuple[str, str]:
    """Subject and body of the email for one entity's HIGH suspects of one day (pure, unit-tested).

    Args:
        entity: Entity id.
        day: The payment date the suspects were paid on (YYYY-MM-DD).
        suspects: Rows of gold/duplicate_suspects (HIGH, that entity and day).

    Returns:
        (subject, body).
    """
    total_usd = sum(float(row["amount_at_risk_usd"]) for row in suspects)
    plural = "s" if len(suspects) != 1 else ""
    subject = f"[AP] {len(suspects)} duplicate payment{plural} {entity} {day}: ${total_usd:,.0f} at risk"

    # One block per suspect, biggest amount first
    lines = [f"High-confidence duplicate payments made by {entity} on {day}:", ""]
    for row in sorted(suspects, key=lambda r: -float(r["amount_at_risk_usd"])):
        at_risk = f"{float(row['amount_at_risk']):,.2f} {row['currency']} (USD {float(row['amount_at_risk_usd']):,.2f})"
        lines += [
            f"- {row['supplier_id']}: {row['rule']}",
            f"    paid again: {row['suspect_payment_id']} (invoice typed as {row['suspect_invoice_number']!r})",
            f"    first paid: {row['original_payment_id']} (invoice typed as {row['original_invoice_number']!r})",
            f"    at risk: {at_risk}",
        ]
    lines += [
        "",
        "Action: if the payment hasn't cleared, stop it;",
        "otherwise request a refund or a credit note from the supplier.",
        "Full review list (including medium-confidence suspects): Athena, sql/02_duplicate_review_list.sql.",
    ]
    return subject[:MAX_SUBJECT], "\n".join(lines)


def send_duplicate_alerts(client: Any, topic_arn: str, suspects: list[dict], day: str) -> list[str]:
    """Publish one email per entity with HIGH suspects paid on `day`. Returns the SNS message ids.

    Args:
        client: A boto3 SNS client (tests pass one backed by moto).
        topic_arn: The alerts topic.
        suspects: All rows of gold/duplicate_suspects.
        day: Only suspects paid on this date are alerted (the newest file's day).
    """
    # HIGH suspects of that day, grouped by entity
    by_entity: dict[str, list[dict]] = defaultdict(list)
    for row in suspects:
        if row["confidence"] == HIGH and str(row["dt"]) == day:
            by_entity[row["entity"]].append(row)

    # One message per entity, with the attributes subscriptions filter on
    message_ids = []
    for entity, rows in sorted(by_entity.items()):
        subject, body = duplicate_alert(entity, day, rows)
        response = client.publish(
            TopicArn=topic_arn,
            Subject=subject,
            Message=body,
            MessageAttributes={
                "kind": {"DataType": "String", "StringValue": KIND_DUPLICATE_PAYMENT},
                "entity": {"DataType": "String", "StringValue": entity},
            },
        )
        message_ids.append(response["MessageId"])
    logger.info(
        "Duplicate alerts sent",
        extra={"fields": {"day": day, "entities": sorted(by_entity), "messages": len(message_ids)}},
    )
    return message_ids
