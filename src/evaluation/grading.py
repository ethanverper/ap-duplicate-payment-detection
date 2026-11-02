"""Grade the duplicate detector: what it found, what it missed, and what it raised by mistake.

Why grade at all:
    "26 suspects" means nothing on its own. A detector is judged like a test: of the problems that
    really exist, how many did it catch (recall), and of the alarms it raised, how many were real
    (precision). False alarms waste AP's time; missed duplicates waste money. The simulator wrote
    an answer key (truth/planted_problems.csv) that the pipeline never sees; this module compares.

A planted problem counts as found when the detector flagged its problem payment (the second,
wrong payment) with the rule and confidence that kind of problem should get.
"""

from __future__ import annotations

from dataclasses import dataclass

# What each planted kind should be caught as (src/generators/story.py -> src/glue_jobs/transforms.py)
EXPECTED: dict[str, tuple[str, str]] = {
    "FORMAT_DUPLICATE": ("SAME_INVOICE_RETYPED_NUMBER", "HIGH"),
    "ADVANCE_OVERPAID": ("ADVANCE_THEN_FULL_AMOUNT", "HIGH"),
    "RESUBMITTED_INVOICE": ("RESUBMITTED_UNDER_NEW_NUMBER", "MEDIUM"),
}


@dataclass
class KindScore:
    """Score for one kind of planted problem."""

    kind: str
    planted: int = 0
    found: int = 0
    found_with_wrong_label: int = 0  # flagged, but with another rule or confidence
    planted_at_risk: float = 0.0  # in the invoice currency of each row, converted by the caller
    found_at_risk: float = 0.0

    @property
    def recall_pct(self) -> float:
        """Share of planted problems found, in percent."""
        return round(100 * self.found / self.planted, 1) if self.planted else 100.0


@dataclass
class Grade:
    """The whole report."""

    by_kind: dict[str, KindScore]
    suspects: int
    false_alarms: list[dict]

    @property
    def precision_pct(self) -> float:
        """Share of suspects that were real problems, in percent."""
        real = self.suspects - len(self.false_alarms)
        return round(100 * real / self.suspects, 1) if self.suspects else 100.0

    @property
    def recall_pct(self) -> float:
        """Share of all planted problems that were found, in percent."""
        planted = sum(score.planted for score in self.by_kind.values())
        found = sum(score.found for score in self.by_kind.values())
        return round(100 * found / planted, 1) if planted else 100.0


def grade(suspects: list[dict], truth: list[dict], usd_rate: dict[str, float]) -> Grade:
    """Compare the detector's suspects with the answer key.

    Args:
        suspects: Rows of gold/duplicate_suspects (suspect_payment_id, rule, confidence, amount_at_risk_usd).
        truth: Rows of truth/planted_problems.csv (kind, problem_payment_id, amount_at_risk, currency).
        usd_rate: Currency -> USD rate, to compare money in one unit.

    Returns:
        Per-kind recall, overall precision and recall, and the false alarms themselves.
    """
    # Index the suspects by the payment they flag
    by_payment = {row["suspect_payment_id"]: row for row in suspects}
    planted_payments = {row["problem_payment_id"] for row in truth}

    # Each planted problem: found with the right label, found with another label, or missed
    scores = {kind: KindScore(kind) for kind in EXPECTED}
    for problem in truth:
        score = scores[problem["kind"]]
        score.planted += 1
        score.planted_at_risk += float(problem["amount_at_risk"]) * usd_rate[problem["currency"]]
        hit = by_payment.get(problem["problem_payment_id"])
        if hit is None:
            continue
        if (hit["rule"], hit["confidence"]) == EXPECTED[problem["kind"]]:
            score.found += 1
            score.found_at_risk += float(hit["amount_at_risk_usd"])
        else:
            score.found_with_wrong_label += 1

    # Every suspect that matches no planted problem is a false alarm
    false_alarms = [row for row in suspects if row["suspect_payment_id"] not in planted_payments]
    return Grade(scores, len(suspects), false_alarms)


def report(result: Grade) -> str:
    """A plain-text table of the grade, for the terminal and the README."""
    lines = [
        f"{'Planted problem':<22} {'Planted':>7} {'Found':>6} {'Recall':>7} {'$ at risk planted':>18} {'$ found':>12}",
    ]
    for score in result.by_kind.values():
        lines.append(
            f"{score.kind:<22} {score.planted:>7} {score.found:>6} {score.recall_pct:>6}% "
            f"{score.planted_at_risk:>18,.0f} {score.found_at_risk:>12,.0f}"
        )
    lines.append(
        f"Suspects raised: {result.suspects} · false alarms: {len(result.false_alarms)} · "
        f"precision {result.precision_pct}% · recall {result.recall_pct}%"
    )
    return "\n".join(lines)
