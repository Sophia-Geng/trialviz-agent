"""Planner prompt. Dimension descriptions come from the registry, so the prompt never drifts
from what the pipeline can execute. Few-shot plans are real QueryPlanLLM objects and are
checked against the business rules in tests."""

from __future__ import annotations

from datetime import date

from app.data.dimensions import DIMENSIONS
from app.schemas.plan import Cohort, MeasureSpec, NetworkSpec, PlanFilters, QueryPlanLLM


def _f(**kw) -> PlanFilters:
    return PlanFilters(**{**{name: None for name in PlanFilters.model_fields}, **kw})


def _plan(**kw) -> QueryPlanLLM:
    base = dict(
        answer_mode="visualization", unsupported_reason=None, cohorts=[], shared_filters=_f(),
        group_by=None, split_by=None, measure=None, network=None, top_n=None,
    )
    return QueryPlanLLM(**{**base, **kw})


FEW_SHOTS: list[tuple[str, QueryPlanLLM]] = [
    (
        "How has the number of trials for Keytruda changed per year since 2015?",
        _plan(intent="trend", viz_type="time_series", group_by="start_year",
              shared_filters=_f(intervention="Keytruda", start_year=2015),
              interpretation="Number of trials involving Keytruda (pembrolizumab) per start year since 2015."),
    ),
    (
        "How are Alzheimer's disease trials distributed across phases?",
        _plan(intent="distribution", viz_type="bar_chart", group_by="phase",
              shared_filters=_f(condition="Alzheimer's disease"),
              interpretation="Alzheimer's disease trials counted by phase."),
    ),
    (
        "What are the most common intervention types in type 2 diabetes trials?",
        _plan(intent="distribution", viz_type="bar_chart", group_by="intervention_type",
              shared_filters=_f(condition="type 2 diabetes"),
              interpretation="Type 2 diabetes trials counted by intervention type."),
    ),
    (
        "Compare phases for trials involving pembrolizumab vs nivolumab",
        _plan(intent="comparison", viz_type="grouped_bar_chart", group_by="phase",
              cohorts=[Cohort(label="Pembrolizumab", filters=_f(intervention="pembrolizumab")),
                       Cohort(label="Nivolumab", filters=_f(intervention="nivolumab"))],
              interpretation="Phase distribution of pembrolizumab trials vs nivolumab trials."),
    ),
    (
        "Compare sponsor categories across lung cancer and breast cancer trials since 2018",
        _plan(intent="comparison", viz_type="grouped_bar_chart", group_by="sponsor_class",
              cohorts=[Cohort(label="Lung cancer", filters=_f(condition="lung cancer")),
                       Cohort(label="Breast cancer", filters=_f(condition="breast cancer"))],
              shared_filters=_f(start_year=2018),
              interpretation="Lead sponsor categories of lung vs breast cancer trials started since 2018."),
    ),
    (
        "Which 10 countries have the most recruiting trials for multiple sclerosis?",
        _plan(intent="geographic", viz_type="bar_chart", group_by="recruiting_country", top_n=10,
              shared_filters=_f(condition="multiple sclerosis", status=["RECRUITING"]),
              interpretation="Top 10 countries by number of recruiting multiple sclerosis trials (recruiting sites)."),
    ),
    (
        "Show a network of sponsors and drugs for melanoma trials",
        _plan(intent="network", viz_type="network_graph",
              network=NetworkSpec(source_dim="lead_sponsor", target_dim="drug"),
              shared_filters=_f(condition="melanoma"),
              interpretation="Lead sponsors linked to the drugs they test in melanoma trials; edge weight = shared trials."),
    ),
    (
        "Which drugs are most often combined with nivolumab?",
        _plan(intent="network", viz_type="network_graph",
              network=NetworkSpec(source_dim="drug", target_dim="drug"),
              shared_filters=_f(intervention="nivolumab"),
              interpretation="Drug co-occurrence network in trials involving nivolumab."),
    ),
    (
        "What is the distribution of enrollment sizes in phase 3 asthma trials?",
        _plan(intent="distribution", viz_type="histogram", measure=MeasureSpec(x="enrollment", y=None),
              shared_filters=_f(condition="asthma", phase=["PHASE3"]),
              interpretation="Histogram of enrollment counts for phase 3 asthma trials."),
    ),
    (
        "Is there a relationship between enrollment and trial duration for psoriasis trials?",
        _plan(intent="correlation", viz_type="scatter_plot", measure=MeasureSpec(x="enrollment", y="duration_months"),
              shared_filters=_f(condition="psoriasis"),
              interpretation="One point per psoriasis trial: enrollment vs months from start to completion."),
    ),
    (
        "How many trials are currently recruiting for ALS?",
        _plan(answer_mode="single_value", intent="count", viz_type="metric",
              shared_filters=_f(condition="amyotrophic lateral sclerosis", status=["RECRUITING"]),
              interpretation="Number of ALS trials with overall status RECRUITING."),
    ),
    (
        "Is semaglutide better than tirzepatide for weight loss?",
        _plan(answer_mode="unsupported", intent="comparison", viz_type=None,
              unsupported_reason="Registry records describe trial designs, not comparative efficacy results. "
              "Try: 'Compare phases of semaglutide vs tirzepatide trials'.",
              interpretation="Efficacy comparison between two drugs."),
    ),
]


def _dimension_table() -> str:
    rows = [f"- {d.name.value} ({d.kind.value}{', multi-valued' if d.multi_valued else ''}): {d.description}"
            for d in DIMENSIONS.values()]
    return "\n".join(rows)


SYSTEM_PROMPT = f"""You turn questions about clinical trials into a QueryPlan for a system that queries the
ClinicalTrials.gov registry and counts trials. You never answer the question yourself and never output
numbers about trials: deterministic code fetches the records and computes every value.

Today is {date.today().isoformat()}.

## Dimensions (the only values allowed for group_by, split_by, measure and network)
{_dimension_table()}

## Choosing the visualization
- Trend over time ("per year", "over time", "since 2015") -> time_series, group_by=start_year.
- Comparing 2-4 named drugs or conditions -> grouped_bar_chart with one cohort per item.
- Distribution over one categorical dimension -> bar_chart (rankings like "most common", "top" too).
- Two categorical dimensions within one population -> grouped_bar_chart with group_by + split_by.
- Where trials happen -> group_by=country, or recruiting_country when the question is about recruiting.
- Relationships between entities (sponsors <-> drugs, drug combinations, investigators, sites) -> network_graph.
  Use dimension drug (not intervention_name) for drug networks; source_dim == target_dim for co-occurrence.
- One numeric dimension (enrollment, duration_months, site_count) -> histogram with measure.x.
- Two numeric dimensions -> scatter_plot with measure.x and measure.y.
- "How many ..." asking for a single number -> answer_mode=single_value, viz_type=metric.
- Not answerable from registry fields (efficacy, safety outcomes, prices, medical advice, unrelated topics)
  -> answer_mode=unsupported, viz_type=null, and give unsupported_reason with a suggested rephrasing.
  The rephrasing must be answerable with the dimensions above. Eligibility criteria (age, sex, pregnancy,
  inclusion/exclusion), outcomes, results and adverse events are NOT available, so never suggest questions about
  them; suggest e.g. a phase, status, sponsor, country or trend question about the same drug or condition instead.

## Filters
- Copy drug, condition, sponsor and country names as written in the question; do not invent synonyms.
- Put filters shared by all cohorts in shared_filters; use cohorts only for comparisons (2-4 items).
- Only set status / phase / years / study_type when the question asks for them.
- top_n only when the question implies a limit ("top 10"); otherwise null.
- interpretation: one sentence saying exactly what will be counted.
"""


def build_messages(query: str, extra_context: str | None = None) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for question, plan in FEW_SHOTS:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": plan.model_dump_json()})
    content = query if not extra_context else f"{query}\n\n(Structured fields provided by the user: {extra_context})"
    messages.append({"role": "user", "content": content})
    return messages
