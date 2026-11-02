"""The Lambda end to end against simulated S3 and SNS (moto): validated, duplicate, rejected."""

from __future__ import annotations

import json

from src.lambdas.file_validator.handler import ALREADY_LOADED, DUPLICATE, REJECTED, VALIDATED, handler
from tests.lambdas.conftest import BUCKET, landing_key


def _land(env: dict, key: str, text: str) -> dict:
    """Upload a file to landing/ and deliver the S3 event the bucket would send."""
    env["s3"].put_object(Bucket=BUCKET, Key=key, Body=text.encode("utf-8"))
    event = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": key.replace(" ", "+")}}}]}
    return handler(event)["files"][0]


def _emails(env: dict) -> list[dict]:
    """Messages SNS delivered (the subscribed queue receives the SNS envelope)."""
    messages = env["sqs"].receive_message(QueueUrl=env["queue_url"], MaxNumberOfMessages=10).get("Messages", [])
    return [json.loads(message["Body"]) for message in messages]


def test_a_valid_file_is_copied_to_validated_with_its_fingerprint(aws_env, mx_file) -> None:
    result = _land(aws_env, landing_key(), mx_file)

    assert result["outcome"] == VALIDATED
    assert result["target"] == "validated/ap/entity=MX01/dt=2026-10-23/MX01_ap_20261023.csv"
    head = aws_env["s3"].head_object(Bucket=BUCKET, Key=result["target"])
    assert len(head["Metadata"]["sha256"]) == 64
    assert int(head["Metadata"]["record-count"]) == result["records"]
    assert _emails(aws_env) == []  # nothing to tell AP


def test_the_same_file_sent_twice_is_loaded_once(aws_env, mx_file) -> None:
    """The ERP retries an upload: same bytes, so nothing happens and nobody is alerted."""
    _land(aws_env, landing_key(), mx_file)
    second = _land(aws_env, landing_key(), mx_file)

    assert second["outcome"] == DUPLICATE
    assert _emails(aws_env) == []


def test_a_different_file_for_a_loaded_day_is_never_silently_swapped(aws_env, mx_file) -> None:
    """Same entity and day, other content: refused and escalated, the loaded copy is untouched."""
    first = _land(aws_env, landing_key(), mx_file)
    lines = mx_file.splitlines()
    altered = "\n".join(lines[:2] + lines[3:-1]) + "\n" + lines[-1] + "\n"  # one row fewer
    second = _land(aws_env, landing_key(), altered)

    assert second["outcome"] == REJECTED
    assert ALREADY_LOADED in second["issues"]
    kept = aws_env["s3"].get_object(Bucket=BUCKET, Key=first["target"])["Body"].read().decode("utf-8")
    assert kept == mx_file


def test_a_broken_file_is_quarantined_with_reasons_and_ap_is_emailed(aws_env, mx_file) -> None:
    """A file cut in transit: copied to rejected/, reasons next to it, one email with the cause."""
    cut = "\n".join(mx_file.splitlines()[:-10]) + "\n"
    result = _land(aws_env, landing_key(), cut)

    assert result["outcome"] == REJECTED
    reason = json.loads(aws_env["s3"].get_object(Bucket=BUCKET, Key=f"{result['target']}.reason.json")["Body"].read())
    assert reason["issues"][0]["code"] == "MISSING_TRAILER"

    [email] = _emails(aws_env)
    assert email["Subject"].startswith("[AP] File rejected: MX01 2026-10-23")
    assert "MISSING_TRAILER" in email["Message"]
    assert email["MessageAttributes"]["kind"]["Value"] == "FILE_REJECTED"
    assert email["MessageAttributes"]["entity"]["Value"] == "MX01"


def test_a_file_with_a_meaningless_name_is_quarantined_as_unknown(aws_env, mx_file) -> None:
    result = _land(aws_env, "landing/ap/entity=MX01/payments final.csv", mx_file)

    assert result["outcome"] == REJECTED
    assert result["target"] == "rejected/ap/entity=unknown/payments final.csv"


def test_a_huge_object_is_refused_without_reading_it(aws_env, mx_file, monkeypatch) -> None:
    """The size limit comes from configuration (here lowered to make the test file "huge")."""
    from src.lambdas.file_validator import handler as module

    monkeypatch.setenv("MAX_FILE_MB", "0.001")
    module._settings.cache_clear()
    result = _land(aws_env, landing_key(), mx_file)

    assert result["outcome"] == REJECTED
    assert result["issues"] == ["TOO_LARGE"]
