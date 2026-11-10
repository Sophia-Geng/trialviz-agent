"""Keyword fallback used when the LLM is unavailable or keeps producing invalid plans.

Deliberately simple: it never extracts entities from free text (that is exactly
what it cannot do reliably), it only uses explicit request fields plus a few
keyword rules to pick the chart.
"""

from __future__ import annotations

import re

from app.schemas.enums import DimensionName as D
from app.schemas.enums import VizType
from app.schemas.plan import MeasureSpec, NetworkSpec, PlanFilters, QueryPlanLLM

_RULES: list[tuple[re.Pattern, dict]] = [
    (re.compile(r"network|co-?occur|combin|↔|<->|relationship", re.I),
     {"intent": "network", "viz_type": VizType.NETWORK_GRAPH,
      "network": NetworkSpec(source_dim=D.LEAD_SPONSOR, target_dim=D.DRUG)}),
    (re.compile(r"per year|each year|over time|trend|since \d{4}|by year|timeline", re.I),
     {"intent": "trend", "viz_type": VizType.TIME_SERIES, "group_by": D.START_YEAR}),
    (re.compile(r"countr|where|geograph|location", re.I),
     {"intent": "geographic", "viz_type": VizType.BAR_CHART, "group_by": D.COUNTRY}),
    (re.compile(r"enrol|participants|sample size", re.I),
     {"intent": "distribution", "viz_type": VizType.HISTOGRAM, "measure": MeasureSpec(x=D.ENROLLMENT, y=None)}),
    (re.compile(r"sponsor", re.I),
     {"intent": "distribution", "viz_type": VizType.BAR_CHART, "group_by": D.SPONSOR_CLASS}),
    (re.compile(r"intervention type|type of intervention", re.I),
     {"intent": "distribution", "viz_type": VizType.BAR_CHART, "group_by": D.INTERVENTION_TYPE}),
    (re.compile(r"status", re.I),
     {"intent": "distribution", "viz_type": VizType.BAR_CHART, "group_by": D.OVERALL_STATUS}),
]

_DEFAULT = {"intent": "distribution", "viz_type": VizType.BAR_CHART, "group_by": D.PHASE}


def fallback_plan(query: str) -> QueryPlanLLM:
    chosen = next((spec for pattern, spec in _RULES if pattern.search(query)), _DEFAULT)
    if chosen["viz_type"] is VizType.BAR_CHART and chosen.get("group_by") is D.COUNTRY and re.search(r"recruit", query, re.I):
        chosen = {**chosen, "group_by": D.RECRUITING_COUNTRY}
    empty = PlanFilters(**{name: None for name in PlanFilters.model_fields})
    return QueryPlanLLM(
        answer_mode="visualization",
        unsupported_reason=None,
        cohorts=[],
        shared_filters=empty,
        split_by=None,
        top_n=None,
        **{"group_by": None, "measure": None, "network": None, **chosen},
        interpretation=f"Keyword fallback: {chosen['viz_type'].value} chosen from the wording of the question.",
    )
