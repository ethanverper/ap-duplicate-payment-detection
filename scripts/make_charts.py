"""Draw the README charts from the Athena results of the real run (docs/images/results/*.csv).

Why from the CSVs and not from AWS:
    The deploy saved every query's result (`make queries`) and the stack was destroyed afterwards.
    The charts are drawn from those saved results, so they show exactly what Athena returned, and
    anyone can redraw them without an AWS account.

Style (the same rules for every chart):
    - one question per chart, written as its title; the data source under it;
    - neutral gray for context, one color for the finding;
    - every bar labelled with its value and unit; light grid, no chart junk.

Usage:
    python -m scripts.make_charts --results docs/images/results --out docs/images
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # draw to files, no window

import matplotlib.pyplot as plt  # noqa: E402  (must come after choosing the backend)
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

# Colors (validated for color-vision deficiency and contrast against the white surface)
HIGHLIGHT = "#2a78d6"  # the finding / first category
SECOND = "#eb6834"  # second category
CONTEXT = "#c9c7c1"  # everything else
INK, MUTED = "#0b0b0b", "#52514e"

# How each detection rule is named for a business reader
RULE_LABELS = {
    "SAME_INVOICE_RETYPED_NUMBER": "Same invoice, number retyped",
    "ADVANCE_THEN_FULL_AMOUNT": "Advance, then the full amount",
    "RESUBMITTED_UNDER_NEW_NUMBER": "Resubmitted under a new number",
}


def read_csv(path: Path) -> list[dict]:
    """Rows of one saved Athena result."""
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _style(ax) -> None:
    """Recessive axes: no box, a light vertical grid behind the bars."""
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.grid(axis="x", color="#e6e5e0", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, length=0)


def _finish(fig: Figure, title: str, source: str) -> Figure:
    """Title as the question, source as the subtitle."""
    fig.suptitle(title, x=0.02, ha="left", fontsize=13, fontweight="bold", color=INK)
    fig.text(0.02, 0.905, source, ha="left", fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, 0.89))
    return fig


def at_risk_by_rule(review_list: list[dict]) -> Figure:
    """KPI 1: money at risk per detection rule, colored by confidence (sql/02)."""
    # Sum the money and count the suspects per rule
    usd: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    confidence: dict[str, str] = {}
    for row in review_list:
        usd[row["rule"]] += float(row["amount_at_risk_usd"])
        count[row["rule"]] += 1
        confidence[row["rule"]] = row["confidence"]

    # Biggest at the top
    rules = sorted(usd, key=usd.get)
    fig, ax = plt.subplots(figsize=(9, 3.6))
    colors = [HIGHLIGHT if confidence[rule] == "HIGH" else SECOND for rule in rules]
    bars = ax.barh([RULE_LABELS[rule] for rule in rules], [usd[r] for r in rules], color=colors, height=0.55)
    ax.bar_label(
        bars, labels=[f"${usd[r]:,.0f} · {count[r]} payments" for r in rules], padding=4, fontsize=9, color=INK
    )

    # The legend explains the color: proven overpayment vs to be reviewed
    handles = [
        Patch(color=HIGHLIGHT, label="HIGH confidence: proven overpayment"),
        Patch(color=SECOND, label="MEDIUM confidence: review before claiming"),
    ]
    ax.legend(handles=handles, frameon=False, fontsize=9, loc="lower right")
    ax.set_xlabel("Money at risk (USD)", color=MUTED)
    ax.set_xlim(0, max(usd.values()) * 1.45)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:,.0f}K"))
    _style(ax)
    total = sum(usd.values())
    return _finish(fig, f"Where the ${total / 1000:,.0f}K at risk comes from: 27 of 27 planted problems found",
                   "Athena, real run · sql/02_duplicate_review_list.sql")  # fmt: skip


def on_time_by_supplier(performance: list[dict], target_pct: float) -> Figure:
    """KPI 2: on-time payment % per supplier and entity, the worst highlighted (sql/03)."""
    # One bar per entity and supplier, worst at the top
    rows = [row for row in performance if row["level"] == "3 Supplier"]
    rows.sort(key=lambda row: float(row["on_time_pct"]), reverse=True)
    labels = [f"{row['supplier_id'].removeprefix('SUP-').title()} ({row['entity']})" for row in rows]
    values = [float(row["on_time_pct"]) for row in rows]

    # Only suppliers under the target carry the finding color
    fig, ax = plt.subplots(figsize=(9, 6.2))
    colors = [HIGHLIGHT if value < target_pct else CONTEXT for value in values]
    bars = ax.barh(labels, values, color=colors, height=0.62)
    # A 0% bar has no length: its label says what it means
    zero_note = "  ← every payment late"
    labels_pct = [f"{value:.0f}%" + (zero_note if value == 0 else "") for value in values]
    ax.bar_label(bars, labels=labels_pct, padding=3, fontsize=8, color=INK)

    # The target the AP manager watches
    ax.axvline(target_pct, color=MUTED, linestyle="--", linewidth=1)
    ax.text(target_pct, len(rows) - 0.3, f" target {target_pct:.0f}%", color=MUTED, fontsize=9, va="bottom")
    ax.set_xlim(0, 112)
    ax.set_xlabel("Payments made on or before the due date (%)", color=MUTED)
    ax.tick_params(axis="y", labelsize=8)
    _style(ax)
    return _finish(fig, "Which suppliers are we paying late?",
                   "Athena, real run · sql/03_on_time_and_dpo.sql · settling payments over 90 days")  # fmt: skip


def discounts_lost(discounts: list[dict], top: int = 8) -> Figure:
    """KPI 4: early-payment discounts lost per supplier, by entity (sql/04)."""
    # Supplier rows, biggest losses at the top
    rows = sorted((r for r in discounts if r["level"] == "3 Supplier"), key=lambda r: float(r["discount_lost_usd"]))
    rows = rows[-top:]
    labels = [f"{row['supplier_id'].removeprefix('SUP-').title()} ({row['entity']})" for row in rows]
    values = [float(row["discount_lost_usd"]) for row in rows]
    colors = [HIGHLIGHT if row["entity"] == "US01" else SECOND for row in rows]

    fig, ax = plt.subplots(figsize=(9, 4.4))
    bars = ax.barh(labels, values, color=colors, height=0.6)
    ax.bar_label(bars, labels=[f"${value:,.0f}" for value in values], padding=4, fontsize=9, color=INK)

    # Identity by legend (and the entity in every label), never by color alone
    by_entity = {r["entity"]: float(r["discount_lost_usd"]) for r in discounts if r["level"] == "1 Entity"}
    handles = [
        Patch(color=HIGHLIGHT, label=f"US01: pays on Fridays only · ${by_entity.get('US01', 0) / 1000:,.0f}K lost"),
        Patch(color=SECOND, label=f"MX01: pays every business day · ${by_entity.get('MX01', 0) / 1000:,.0f}K lost"),
    ]
    ax.legend(handles=handles, frameon=False, fontsize=9, loc="lower right")
    ax.set_xlim(0, max(values) * 1.3)
    ax.set_xlabel("Early-payment discounts lost (USD)", color=MUTED)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:,.0f}K"))
    _style(ax)
    return _finish(fig, "How much do we throw away by paying after the discount window?",
                   "Athena, real run · sql/04_discounts_lost.sql · top suppliers")  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    """Read the saved results, draw the three charts."""
    parser = argparse.ArgumentParser(description="Draw the README charts from the saved Athena results.")
    parser.add_argument("--results", type=Path, default=Path("docs/images/results"))
    parser.add_argument("--out", type=Path, default=Path("docs/images"))
    parser.add_argument("--on-time-target", type=float, default=None, help="default: kpis.on_time_alert_pct")
    args = parser.parse_args(argv)

    # The on-time target comes from the central configuration unless given
    from src.common.settings import settings

    target = args.on_time_target or float(settings()["kpis"]["on_time_alert_pct"])

    charts = {
        "duplicates_at_risk.png": at_risk_by_rule(read_csv(args.results / "02_duplicate_review_list.csv")),
        "on_time_by_supplier.png": on_time_by_supplier(read_csv(args.results / "03_on_time_and_dpo.csv"), target),
        "discounts_lost.png": discounts_lost(read_csv(args.results / "04_discounts_lost.csv")),
    }
    for name, figure in charts.items():
        figure.savefig(args.out / name, dpi=150)
        print(f"saved {args.out / name}")


if __name__ == "__main__":
    main()
