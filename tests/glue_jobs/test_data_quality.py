"""The Glue Data Quality ruleset agrees with the file contract, master data and the curated table."""

from __future__ import annotations

import re
from pathlib import Path

from src.common.schema import PAYMENT_TYPES
from src.generators.reference import ENTITIES
from src.glue_jobs import data_quality

RULESET_FILE = Path(__file__).resolve().parents[2] / "dq" / "curated_payments.dqdl"


def _ruleset() -> str:
    return data_quality.strip_comments(RULESET_FILE.read_text(encoding="utf-8"))


def _allowed(column: str) -> set[str]:
    """The values a `ColumnValues "<column>" in [...]` rule allows."""
    rule = next(r for r in data_quality.rules_of(_ruleset()) if r.startswith(f'ColumnValues "{column}" in'))
    return set(re.findall(r'"([^"]+)"', rule.split(" in ", 1)[1]))


def test_ruleset_parses_into_rules() -> None:
    rules = data_quality.rules_of(_ruleset())
    assert 'IsUnique "payment_id"' in rules
    assert len(rules) >= 10


def test_value_lists_match_the_contract_and_master_data() -> None:
    """If someone adds an entity or a payment type, the ruleset must change with it."""
    assert _allowed("payment_type") == set(PAYMENT_TYPES)
    assert _allowed("entity") == set(ENTITIES)
    assert _allowed("currency") == {entity.currency for entity in ENTITIES.values()}


def test_every_column_in_the_ruleset_exists_in_the_curated_table(tables) -> None:
    """A rule on a column that doesn't exist would fail on AWS, after paying for the run."""
    columns = set(tables["curated/payments"].columns)
    # The column is the first quoted word after the rule type: IsUnique "payment_id", ColumnValues "entity" in [...]
    rules = data_quality.rules_of(_ruleset())
    referenced = {match[1] for rule in rules if (match := re.match(r'^\w+\s+"([a-z_]+)"', rule))}
    assert referenced <= columns, referenced - columns


def test_outside_glue_the_evaluation_is_skipped(spark, tables) -> None:
    """Locally the Glue-only libraries don't exist: nothing is evaluated, and it says so."""
    assert data_quality.evaluate(spark, tables["curated/payments"], _ruleset()) is None
