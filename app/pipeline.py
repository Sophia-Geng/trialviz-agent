"""Execute a validated QueryPlan: fetch -> aggregate -> build spec -> assemble meta.

Everything here is deterministic; the LLM's only input is the plan itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.aggregate.engine import group_by, partition
from app.ctgov.client import CtgovClient, FetchResult
from app.ctgov.query_builder import build_params, merge_filters
from app.data.dimensions import DIMENSIONS, api_fields
from app.schemas.enums import AnswerMode, DimensionName, VizType
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

    notes = [*plan.notes, *built.notes, *_cohort_notes(cohorts)]
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
            record_counts=RecordCounts(
                total_matching=sum(c.fetch.total for c in cohorts),
                fetched=sum(len(c.fetch.studies) for c in cohorts),
                truncated=any(c.fetch.truncated for c in cohorts),
                count_method="full_scan",
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


def _fetch_cohorts(plan: QueryPlan, client: CtgovClient) -> list[CohortData]:
    dims = [d for d in (plan.group_by, plan.split_by) if d is not None]
    fields = api_fields(dims)
    cohorts = plan.cohorts or [Cohort(label=ALL_TRIALS, filters=empty_filters())]
    out = []
    for cohort in cohorts:
        filters = merge_filters(plan.shared_filters, cohort.filters)
        params = build_params(filters)
        out.append(CohortData(cohort.label, filters, params, client.fetch_studies(params, fields, plan.max_records)))
    return out


def _build(plan: QueryPlan, cohorts: list[CohortData]) -> builder.Built:
    title = _title(plan, cohorts)
    match plan.viz_type:
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
            series = [(label, group_by(part, plan.group_by)) for label, part in partition(cohorts[0].fetch.studies, plan.split_by)]
            return builder.grouped_bar_chart(series, title, plan.top_n, split_by=plan.split_by)
    raise NotImplementedError(f"viz_type {plan.viz_type} is not implemented yet")


def _title(plan: QueryPlan, cohorts: list[CohortData]) -> str:
    what = DIMENSIONS[plan.group_by].label.lower() if plan.group_by else "trials"
    if len(cohorts) > 1:
        scope = " vs ".join(c.label for c in cohorts)
    else:
        f = cohorts[0].filters
        scope = " · ".join(x for x in (f.intervention, f.condition, f.sponsor, f.country) if x) or ALL_TRIALS
    prefix = "Trials per start year" if plan.group_by is DimensionName.START_YEAR else f"Trials by {what}"
    return f"{prefix}: {scope}"


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
            notes=[plan.unsupported_reason or "This question cannot be answered from ClinicalTrials.gov data.", *plan.notes],
            source=SourceInfo(
                api_version=version.get("apiVersion"),
                data_timestamp=version.get("dataTimestamp"),
                retrieved_at=datetime.now().astimezone(),
            ),
        ),
    )
