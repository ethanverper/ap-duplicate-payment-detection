"""Shared fixtures: a real generated ERP file to break in many ways, and a simulated AWS (moto)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import boto3
import pytest
from moto import mock_aws

from src.common import aws
from src.generators.files import render_file
from src.generators.simulator import simulate
from src.lambdas.file_validator import handler

# One business day with plenty of payments in both entities
FILE_DAY = date(2026, 10, 23)  # a Friday: US01 pays too
BUCKET = "ap-lake-test"


@pytest.fixture(scope="session")
def day_rows() -> dict[str, list[dict]]:
    """The payment rows of FILE_DAY per entity, from a real simulation."""
    result = simulate(date(2026, 9, 1), date(2026, 10, 30), seed=42)
    rows: dict[str, list[dict]] = {"MX01": [], "US01": []}
    for payment in result.payments:
        if payment.payment_date == FILE_DAY:
            rows[payment.invoice.entity].append(payment.to_row())
    return rows


@pytest.fixture
def mx_file(day_rows) -> str:
    """A valid MX01 file for FILE_DAY, as text."""
    return render_file(day_rows["MX01"])


def landing_key(entity: str = "MX01", day: date = FILE_DAY) -> str:
    """The landing key the ERP would use."""
    return f"landing/ap/entity={entity}/{entity}_ap_{day:%Y%m%d}.csv"


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Moto S3 + SNS (with an SQS queue subscribed, to read the emails), and the Lambda's env vars."""
    for name, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                        "AWS_DEFAULT_REGION": "us-east-1"}.items():  # fmt: skip
        monkeypatch.setenv(name, value)

    with mock_aws():
        # The bucket, the alerts topic, and a queue that receives what the topic publishes
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        sns, sqs = boto3.client("sns"), boto3.client("sqs")
        topic_arn = sns.create_topic(Name="ap-alerts")["TopicArn"]
        queue_url = sqs.create_queue(QueueName="inbox")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn,
                      Attributes={"RawMessageDelivery": "false"})  # fmt: skip

        # The environment Terraform will set
        monkeypatch.setenv("KNOWN_ENTITIES", "MX01,US01")
        monkeypatch.setenv("MAX_FILE_MB", "50")
        monkeypatch.setenv("VALIDATED_PREFIX", "validated/ap")
        monkeypatch.setenv("REJECTED_PREFIX", "rejected/ap")
        monkeypatch.setenv("ALERT_TOPIC_ARN", topic_arn)

        # Cached clients and settings must be rebuilt inside the mock
        aws.get_client.cache_clear()
        handler._settings.cache_clear()
        yield {"s3": s3, "sqs": sqs, "queue_url": queue_url}
        aws.get_client.cache_clear()
        handler._settings.cache_clear()
