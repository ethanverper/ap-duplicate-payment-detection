"""The live-demo files: what each upload of the demo must make the file validator do."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.generators.history import main as generate
from src.lambdas.file_validator import validation as v

DEMO_DAY = date(2026, 11, 4)  # a Wednesday: MX01 pays, US01 (Fridays only) has an empty file


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("demo")
    generate(["--demo", "--last-day", DEMO_DAY.isoformat(), "--out", str(root)])
    return root


def _check(path: Path, entity: str) -> set[str]:
    identity = v.FileIdentity(entity, DEMO_DAY, path.name)
    return {issue.code for issue in v.check_content(path.read_bytes(), identity).issues}


def test_the_demo_day_is_kept_out_of_the_history(out) -> None:
    """The history stops the day before; the demo day exists only in demo/."""
    names = sorted(p.name for p in (out / "landing" / "ap" / "entity=MX01").glob("*.csv"))
    assert names[-1] == "MX01_ap_20261103.csv"
    assert (out / "demo" / "valid" / "MX01_ap_20261104.csv").exists()


def test_the_correct_demo_files_pass_and_the_edited_ones_fail(out) -> None:
    """Steps 1, 2 and 4 of the demo: edited copy rejected, correct files validated."""
    for entity in ("MX01", "US01"):
        assert _check(out / "demo" / "valid" / f"{entity}_ap_20261104.csv", entity) == set()
    assert _check(out / "demo" / "corrupt" / "MX01_ap_20261104.csv", "MX01") == {v.TRAILER_TOTAL_MISMATCH}
    assert _check(out / "demo" / "corrupt" / "US01_ap_20261104.csv", "US01") == {v.MISSING_TRAILER}


def test_the_injected_duplicate_is_in_todays_file_and_the_answer_key(out) -> None:
    """Step 5: one invoice paid earlier is paid again today under a retyped number."""
    truth = (out / "truth" / "planted_problems.csv").read_text(encoding="utf-8").splitlines()
    injected = truth[-1].split(",")
    today = (out / "demo" / "valid" / "MX01_ap_20261104.csv").read_text(encoding="utf-8")

    assert injected[0] == "FORMAT_DUPLICATE" and injected[1] == "MX01"
    assert injected[4] in today  # the duplicate payment id is a row of today's file
    assert injected[3] not in today  # the original was paid on an earlier day
