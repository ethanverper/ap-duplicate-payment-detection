"""The duplicate-payment email: only HIGH suspects of the day, one message per entity (moto SNS)."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from src.glue_jobs.alerts import duplicate_alert, send_duplicate_alerts


def _suspect(entity: str, confidence: str, dt: str, usd: float, payment: str) -> dict:
    return {
        "entity": entity, "confidence": confidence, "dt": dt, "supplier_id": "SUP-PRECISA",
        "rule": "SAME_INVOICE_RETYPED_NUMBER", "suspect_payment_id": payment, "original_payment_id": "PAY-1",
        "suspect_invoice_number": "fac 457", "original_invoice_number": "FAC-0457",
        "amount_at_risk": usd * 18, "currency": "MXN", "amount_at_risk_usd": usd,
    }  # fmt: skip


SUSPECTS = [
    _suspect("MX01", "HIGH", "2026-10-30", 9000.0, "PAY-A"),
    _suspect("MX01", "HIGH", "2026-10-30", 1000.0, "PAY-B"),
    _suspect("US01", "HIGH", "2026-10-30", 500.0, "PAY-C"),
    _suspect("MX01", "MEDIUM", "2026-10-30", 7000.0, "PAY-D"),  # medium: review list only
    _suspect("MX01", "HIGH", "2026-10-29", 3000.0, "PAY-E"),  # an earlier day: already alerted then
]


def test_the_email_states_supplier_numbers_and_money() -> None:
    subject, body = duplicate_alert("MX01", "2026-10-30", SUSPECTS[:2])
    assert subject == "[AP] 2 duplicate payments MX01 2026-10-30: $10,000 at risk"
    assert "'fac 457'" in body and "'FAC-0457'" in body
    assert body.index("PAY-A") < body.index("PAY-B")  # biggest first


@pytest.fixture
def sns_inbox(monkeypatch):
    """A moto SNS topic with an SQS queue subscribed, to read what was published."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        sns, sqs = boto3.client("sns"), boto3.client("sqs")
        topic = sns.create_topic(Name="ap-alerts")["TopicArn"]
        queue = sqs.create_queue(QueueName="inbox")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=arn)
        yield sns, sqs, topic, queue


def test_only_high_suspects_of_the_day_are_sent_one_email_per_entity(sns_inbox) -> None:
    sns, sqs, topic, queue = sns_inbox
    ids = send_duplicate_alerts(sns, topic, SUSPECTS, "2026-10-30")

    messages = [json.loads(m["Body"]) for m in sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10)["Messages"]]
    assert len(ids) == len(messages) == 2
    by_entity = {m["MessageAttributes"]["entity"]["Value"]: m for m in messages}
    assert set(by_entity) == {"MX01", "US01"}
    assert "PAY-D" not in by_entity["MX01"]["Message"] and "PAY-E" not in by_entity["MX01"]["Message"]
    assert by_entity["MX01"]["MessageAttributes"]["kind"]["Value"] == "DUPLICATE_PAYMENT"


def test_a_day_without_high_suspects_sends_nothing(sns_inbox) -> None:
    sns, _sqs, topic, _queue = sns_inbox
    assert send_duplicate_alerts(sns, topic, SUSPECTS, "2026-10-01") == []
