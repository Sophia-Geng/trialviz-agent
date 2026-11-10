"""Run hand-written QueryPlans through the real pipeline (no LLM) against the live API.

Usage:
    python scripts/run_plan_demo.py            # all plans, print a summary
    python scripts/run_plan_demo.py phases -v  # one plan, print the full JSON response
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ctgov.client import CtgovClient  # noqa: E402
from app.pipeline import empty_filters, execute_plan  # noqa: E402
from app.schemas.plan import Cohort, NetworkSpec, PlanFilters, QueryPlan  # noqa: E402


def filters(**kw) -> PlanFilters:
    return PlanFilters(**{**empty_filters().model_dump(), **kw})


def plan(**kw) -> QueryPlan:
    base = dict(
        answer_mode="visualization",
        unsupported_reason=None,
        cohorts=[],
        shared_filters=empty_filters(),
        group_by=None,
        split_by=None,
        measure=None,
        network=None,
        top_n=15,
    )
    return QueryPlan(**{**base, **kw})


PLANS = {
    "trend": plan(
        intent="trend",
        viz_type="time_series",
        group_by="start_year",
        shared_filters=filters(intervention="pembrolizumab", start_year=2015),
        interpretation="Number of trials per start year involving pembrolizumab, since 2015.",
    ),
    "phases": plan(
        intent="distribution",
        viz_type="bar_chart",
        group_by="phase",
        shared_filters=filters(condition="Alzheimer's disease"),
        interpretation="Alzheimer's disease trials by phase.",
    ),
    "compare": plan(
        intent="comparison",
        viz_type="grouped_bar_chart",
        group_by="phase",
        cohorts=[
            Cohort(label="Pembrolizumab", filters=filters(intervention="pembrolizumab")),
            Cohort(label="Nivolumab", filters=filters(intervention="nivolumab")),
        ],
        interpretation="Phase distribution of pembrolizumab vs nivolumab trials.",
    ),
    "countries": plan(
        intent="geographic",
        viz_type="bar_chart",
        group_by="recruiting_country",
        shared_filters=filters(condition="breast cancer", status=["RECRUITING"]),
        interpretation="Countries with the most recruiting breast cancer trials.",
    ),
    "sponsor_class": plan(
        intent="comparison",
        viz_type="grouped_bar_chart",
        group_by="sponsor_class",
        cohorts=[
            Cohort(label="Melanoma", filters=filters(condition="melanoma")),
            Cohort(label="Pancreatic cancer", filters=filters(condition="pancreatic cancer")),
        ],
        shared_filters=filters(start_year=2020),
        interpretation="Lead sponsor categories of melanoma vs pancreatic cancer trials since 2020.",
    ),
    "network": plan(
        intent="network",
        viz_type="network_graph",
        network=NetworkSpec(source_dim="lead_sponsor", target_dim="drug"),
        shared_filters=filters(condition="melanoma"),
        interpretation="Lead sponsors linked to the drugs they test in melanoma trials.",
    ),
    "combos": plan(
        intent="network",
        viz_type="network_graph",
        network=NetworkSpec(source_dim="drug", target_dim="drug"),
        shared_filters=filters(intervention="nivolumab"),
        interpretation="Drugs combined with nivolumab in the same trial.",
    ),
}


def main(names: list[str], verbose: bool) -> None:
    client = CtgovClient()
    for name in names:
        resp = execute_plan(PLANS[name], client)
        viz, meta = resp.visualization, resp.meta
        print(f"\n=== {name}: {viz.type} — {viz.title}")
        print(f"records: {meta.record_counts.model_dump()}")
        if viz.type == "network_graph":
            labels = {n.id: n.label for n in viz.data.nodes}
            print(f"  {len(viz.data.nodes)} nodes, {len(viz.data.edges)} edges")
            for e in viz.data.edges[:12]:
                print(f"  {labels[e.source]:<40} — {labels[e.target]:<25} {e.weight:>4}")
            for note in meta.notes:
                print(f"  note: {note}")
            continue
        for row in viz.data[:20]:
            series = f"{row.series:>18} | " if hasattr(row, "series") else ""
            label = getattr(row, "category", None) or getattr(row, "year", None)
            example = row.citations[0].nct_id if row.citations else "-"
            print(f"  {series}{label!s:<28} {row.trial_count:>5}   e.g. {example}")
        for note in meta.notes:
            print(f"  note: {note}")
        if verbose:
            print(resp.model_dump_json(indent=1))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    main(args or list(PLANS), verbose="-v" in sys.argv)
