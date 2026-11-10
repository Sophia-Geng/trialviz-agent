"""Execute a validated QueryPlan: fetch -> aggregate -> build spec -> assemble meta.

Everything here is deterministic; the LLM's only input is the plan itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.aggregate.engine import co_occurrence, group_by, partition
from app.ctgov.client import CtgovClient, FetchResult
from app.ctgov.query_builder import build_params, merge_filters
from app.data.dimensions import DIMENSIONS, api_fields
from app.data.normalize import humanize_enum, normalize_phase
from app.schemas.enums import AnswerMode, VizType
from app.schemas.plan import Cohort, PlanFilters, QueryPlan
from app.schemas.response import Meta, RecordCounts, SourceInfo, VisualizeResponse
from app.viz import builder

ALL_TRIALS = "All trials"


def empty_filters() -> PlanFilters:
    return PlanFilters(**{name: None for name in PlanFilters.model_fields})


@dataclass
class CohortData:
    label: str
    filters: PlanFilters
    params: dict[str, str]
    fetch: FetchResult


def execute_plan(plan: QueryPlan, client: CtgovClient) -> VisualizeResponse:
    if plan.answer_mode is AnswerMode.UNSUPPORTED:
        return _unsupported(plan, client)

    cohorts = _fetch_cohorts(plan, client)
    built = _build(plan, cohorts)

    is_metric = plan.viz_type is VizType.METRIC
    notes = [*plan.notes, *built.notes, *([] if is_metric else _cohort_notes(cohorts))]
    version = client.version()
    return VisualizeResponse(
        visualization=built.visualization,
        meta=Meta(
            answer_mode=plan.answer_mode,
            query_interpretation=plan.interpretation,
            filters_applied=_filters_applied(cohorts),
            plan=plan.model_dump(mode="json"),
            units=built.units,
            sort=built.sort,
            time_granularity=built.time_granularity,
            grouping=built.grouping,
            binning=built.binning,
            record_counts=RecordCounts(
                total_matching=sum(c.fetch.total for c in cohorts),
                fetched=sum(len(c.fetch.studies) for c in cohorts),
                truncated=False if is_metric else any(c.fetch.truncated for c in cohorts),
                count_method="api_total" if is_metric else "full_scan",
            ),
            notes=notes,
            source=SourceInfo(
                api_version=version.get("apiVersion"),
                data_timestamp=version.get("dataTimestamp"),
                retrieved_at=min(c.fetch.retrieved_at for c in cohorts),
            ),
        ),
    )


# --- steps -------------------------------------------------------------------


METRIC_SAMPLE = 5
NETWORK_NODES_PER_TOP_N = 2  # default top_n=15 -> up to 30 nodes


def _fetch_cohorts(plan: QueryPlan, client: CtgovClient) -> list[CohortData]:
    network = [plan.network.source_dim, plan.network.target_dim] if plan.network else []
    measure = [plan.measure.x, plan.measure.y] if plan.measure else []
    dims = [d for d in (plan.group_by, plan.split_by, *network, *measure) if d is not None]
    fields = api_fields(dims)
    # A metric only needs the API's totalCount plus a few example records for citations.
    max_records = METRIC_SAMPLE if plan.viz_type is VizType.METRIC else plan.max_records
    cohorts = plan.cohorts or [Cohort(label=ALL_TRIALS, filters=empty_filters())]
    out = []
    for cohort in cohorts:
        filters = merge_filters(plan.shared_filters, cohort.filters)
        params = build_params(filters)
        out.append(CohortData(cohort.label, filters, params, client.fetch_studies(params, fields, max_records)))
    return out


def _build(plan: QueryPlan, cohorts: list[CohortData]) -> builder.Built:
    title = _title(plan, cohorts)
    match plan.viz_type:
        case VizType.NETWORK_GRAPH:
            graph = co_occurrence(cohorts[0].fetch.studies, plan.network.source_dim, plan.network.target_dim)
            return builder.network_graph(graph, title, max_nodes=plan.top_n * NETWORK_NODES_PER_TOP_N)
        case VizType.METRIC:
            c = cohorts[0]
            return builder.metric(c.fetch.total, c.fetch.studies, title, plan.interpretation)
        case VizType.HISTOGRAM:
            return builder.histogram(cohorts[0].fetch.studies, plan.measure.x, title)
        case VizType.SCATTER_PLOT:
            return builder.scatter_plot(cohorts[0].fetch.studies, plan.measure.x, plan.measure.y, title)
        case VizType.TIME_SERIES:
            f = cohorts[0].filters
            result = group_by(cohorts[0].fetch.studies, plan.group_by)
            return builder.time_series(result, title, f.start_year, f.end_year)
        case VizType.BAR_CHART:
            return builder.bar_chart(group_by(cohorts[0].fetch.studies, plan.group_by), title, plan.top_n)
        case VizType.GROUPED_BAR_CHART if len(cohorts) > 1:
            series = [(c.label, group_by(c.fetch.studies, plan.group_by)) for c in cohorts]
            return builder.grouped_bar_chart(series, title, plan.top_n)
        case VizType.GROUPED_BAR_CHART if plan.split_by is not None:
            series = [
                (label, group_by(part, plan.group_by))
                for label, part in partition(cohorts[0].fetch.studies, plan.split_by)
            ]
            return builder.grouped_bar_chart(series, title, plan.top_n, split_by=plan.split_by)
    raise NotImplementedError(f"viz_type {plan.viz_type} is not implemented yet")


def _scope(f: PlanFilters, entities: bool = True) -> str:
    """Human-readable filter summary for titles, e.g. 'breast cancer · Phase 3 · Recruiting · 2015+'."""
    parts = [x for x in (f.intervention, f.condition, f.sponsor, f.country) if x] if entities else []
    if f.phase:
        parts.append("/".join(normalize_phase([p.value]) for p in f.phase))
    if f.status:
        parts.append("/".join(humanize_enum(s.value) for s in f.status))
    if f.study_type:
        parts.append(humanize_enum(f.study_type.value))
    if f.start_year or f.end_year:
        parts.append(f"{f.start_year or ''}–{f.end_year or ''}" if f.end_year else f"{f.start_year}+")
    return " · ".join(parts)


def _title(plan: QueryPlan, cohorts: list[CohortData]) -> str:
    if len(cohorts) > 1:
        shared = _scope(plan.shared_filters)
        scope = " vs ".join(c.label for c in cohorts) + (f" · {shared}" if shared else "")
    else:
        scope = _scope(cohorts[0].filters) or ALL_TRIALS

    match plan.viz_type:
        case VizType.NETWORK_GRAPH:
            a, b = DIMENSIONS[plan.network.source_dim].label, DIMENSIONS[plan.network.target_dim].label
            return f"{a} ↔ {b} network: {scope}" if a != b else f"{a} co-occurrence network: {scope}"
        case VizType.HISTOGRAM:
            return f"Distribution of {DIMENSIONS[plan.measure.x].label.lower()}: {scope}"
        case VizType.SCATTER_PLOT:
            return f"{DIMENSIONS[plan.measure.x].label} vs {DIMENSIONS[plan.measure.y].label.lower()}: {scope}"
        case VizType.METRIC:
            return f"Matching trials: {scope}"
        case VizType.TIME_SERIES:
            return f"Trials per start year: {scope}"
    return f"Trials by {DIMENSIONS[plan.group_by].label.lower()}: {scope}"


def _filters_applied(cohorts: list[CohortData]) -> dict:
    if len(cohorts) == 1:
        c = cohorts[0]
        return {"filters": c.filters.model_dump(mode="json", exclude_none=True), "api_params": c.params}
    return {
        c.label: {"filters": c.filters.model_dump(mode="json", exclude_none=True), "api_params": c.params}
        for c in cohorts
    }


def _cohort_notes(cohorts: list[CohortData]) -> list[str]:
    notes = []
    for c in cohorts:
        if c.fetch.total == 0:
            notes.append(f"No trials matched '{c.label}'. Check the spelling or broaden the filters.")
        elif c.fetch.truncated:
            notes.append(
                f"'{c.label}': based on the first {len(c.fetch.studies)} of {c.fetch.total} matching trials "
                "(API order, not a random sample)."
            )
    if len(cohorts) > 1:
        ids = [{s["protocolSection"]["identificationModule"]["nctId"] for s in c.fetch.studies} for c in cohorts]
        overlap = set.intersection(*ids)
        if overlap:
            notes.append(f"{len(overlap)} trials match more than one cohort and are counted in each.")
    return notes


def _unsupported(plan: QueryPlan, client: CtgovClient) -> VisualizeResponse:
    version = client.version()
    return VisualizeResponse(
        visualization=None,
        meta=Meta(
            answer_mode=plan.answer_mode,
            query_interpretation=plan.interpretation,
            filters_applied={},
            plan=plan.model_dump(mode="json"),
            notes=[
                plan.unsupported_reason or "This question cannot be answered from ClinicalTrials.gov data.",
                *plan.notes,
            ],
            source=SourceInfo(
                api_version=version.get("apiVersion"),
                data_timestamp=version.get("dataTimestamp"),
                retrieved_at=datetime.now().astimezone(),
            ),
        ),
    )
