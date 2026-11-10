"""Business rules for QueryPlans (PLAN.md §5) and merging of explicit request fields.

`check()` returns human-readable violations; the planner feeds them back to the
LLM for one retry. `apply_request()` enforces "explicit request fields win" and
`apply_scope_guard()` prevents unscoped whole-registry downloads.
"""

from __future__ import annotations

from datetime import date

from app.ctgov.query_builder import has_entity_filter, merge_filters
from app.data.dimensions import DIMENSIONS
from app.schemas.enums import AnswerMode, DimensionKind, StudyType, VizType
from app.schemas.plan import Cohort, PlanFilters, QueryPlan, QueryPlanLLM
from app.schemas.request import MAX_COHORTS, VisualizeRequest

SCOPE_GUARD_YEARS = 5
TOP_N_MAX = 50


class PlanRejected(ValueError):
    """The plan cannot be executed (e.g. an explicit viz_type that contradicts the question)."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _kind(dim) -> DimensionKind | None:
    return DIMENSIONS[dim].kind if dim is not None else None


def check(plan: QueryPlanLLM) -> list[str]:
    errors: list[str] = []
    if plan.answer_mode is AnswerMode.UNSUPPORTED:
        if not plan.unsupported_reason:
            errors.append("unsupported plans must give unsupported_reason")
        return errors

    if plan.answer_mode is AnswerMode.SINGLE_VALUE and plan.viz_type is not VizType.METRIC:
        errors.append("answer_mode single_value requires viz_type metric")
    if plan.viz_type is VizType.METRIC and plan.answer_mode is not AnswerMode.SINGLE_VALUE:
        errors.append("viz_type metric requires answer_mode single_value")
    if plan.viz_type is None:
        errors.append("viz_type is required unless answer_mode is unsupported")
        return errors

    if len(plan.cohorts) > MAX_COHORTS:
        errors.append(f"at most {MAX_COHORTS} cohorts are supported")
    if len(plan.cohorts) == 1:
        errors.append("use shared_filters instead of a single cohort; cohorts are for comparisons (2+)")
    if plan.top_n is not None and not 1 <= plan.top_n <= TOP_N_MAX:
        errors.append(f"top_n must be between 1 and {TOP_N_MAX}")
    for f in [plan.shared_filters, *(c.filters for c in plan.cohorts)]:
        if f.start_year and f.end_year and f.end_year < f.start_year:
            errors.append("end_year must be >= start_year")

    match plan.viz_type:
        case VizType.TIME_SERIES:
            if _kind(plan.group_by) is not DimensionKind.TEMPORAL:
                errors.append("time_series requires a temporal group_by (start_year)")
            if plan.cohorts:
                errors.append("time_series supports a single cohort; compare with grouped_bar_chart instead")
        case VizType.BAR_CHART:
            if _kind(plan.group_by) is not DimensionKind.CATEGORICAL:
                errors.append("bar_chart requires a categorical group_by")
            if plan.cohorts or plan.split_by:
                errors.append("bar_chart has one series; use grouped_bar_chart for cohorts or split_by")
        case VizType.GROUPED_BAR_CHART:
            if _kind(plan.group_by) is not DimensionKind.CATEGORICAL:
                errors.append("grouped_bar_chart requires a categorical group_by")
            if not plan.cohorts and plan.split_by is None:
                errors.append("grouped_bar_chart requires 2+ cohorts or a split_by dimension")
            if plan.split_by is not None and _kind(plan.split_by) is not DimensionKind.CATEGORICAL:
                errors.append("split_by must be categorical")
            if plan.cohorts and plan.split_by is not None:
                errors.append("use either cohorts or split_by, not both")
        case VizType.HISTOGRAM:
            if plan.measure is None or _kind(plan.measure.x) is not DimensionKind.NUMERIC:
                errors.append("histogram requires measure.x to be a numeric dimension")
        case VizType.SCATTER_PLOT:
            if plan.measure is None or plan.measure.y is None:
                errors.append("scatter_plot requires measure.x and measure.y")
            elif {_kind(plan.measure.x), _kind(plan.measure.y)} != {DimensionKind.NUMERIC}:
                errors.append("scatter_plot requires two numeric dimensions")
        case VizType.NETWORK_GRAPH:
            if plan.network is None:
                errors.append("network_graph requires network.source_dim and network.target_dim")
            elif {_kind(plan.network.source_dim), _kind(plan.network.target_dim)} != {DimensionKind.CATEGORICAL}:
                errors.append("network endpoints must be categorical dimensions")
            if plan.group_by is not None:
                errors.append("network_graph must not set group_by")
        case VizType.METRIC:
            if plan.cohorts:
                errors.append("metric answers a single count; cohorts are not supported")
    return errors


def to_internal(plan: QueryPlanLLM, request: VisualizeRequest) -> QueryPlan:
    data = plan.model_dump()
    data["top_n"] = plan.top_n or request.top_n
    return QueryPlan(**data, max_records=request.max_records)


def apply_request(plan: QueryPlan, request: VisualizeRequest) -> QueryPlan:
    """Explicit request fields override what the planner extracted; conflicts become notes."""
    notes = list(plan.notes)
    shared = plan.shared_filters.model_dump()
    cohorts = list(plan.cohorts)

    def override(field: str, value, label: str) -> None:
        if value is None:
            return
        current = shared.get(field)
        if current is not None and _fmt(current).casefold() != _fmt(value).casefold():
            notes.append(f"Using {label} '{_fmt(value)}' from the request instead of '{_fmt(current)}' read from the query.")
        shared[field] = value

    for field, values in (("intervention", request.drug_names), ("condition", request.conditions)):
        if len(values) > 1:
            cohorts = [Cohort(label=v, filters=_only(**{field: v})) for v in values]
            shared[field] = None
        elif values:
            override(field, values[0], field)

    override("sponsor", request.sponsor, "sponsor")
    override("country", request.country, "country")
    override("phase", request.trial_phase, "phase")
    override("status", request.status, "status")
    override("start_year", request.start_year, "start_year")
    override("end_year", request.end_year, "end_year")

    viz_type, answer_mode = plan.viz_type, plan.answer_mode
    if request.viz_type is not None and request.viz_type is not plan.viz_type:
        notes.append(f"viz_type '{request.viz_type}' requested explicitly (planner suggested '{plan.viz_type}').")
        viz_type = request.viz_type
        answer_mode = AnswerMode.SINGLE_VALUE if viz_type is VizType.METRIC else AnswerMode.VISUALIZATION
    if len(cohorts) > 1 and viz_type is VizType.BAR_CHART:
        viz_type = VizType.GROUPED_BAR_CHART
    if len(cohorts) == 1:
        shared.update({k: v for k, v in cohorts[0].filters.model_dump().items() if v is not None})
        cohorts = []

    top_n = request.top_n if "top_n" in request.model_fields_set else plan.top_n
    return plan.model_copy(
        update={
            "shared_filters": PlanFilters(**shared),
            "cohorts": cohorts,
            "viz_type": viz_type,
            "answer_mode": answer_mode,
            "top_n": top_n,
            "max_records": request.max_records,
            "notes": notes,
        }
    )


def apply_scope_guard(plan: QueryPlan) -> QueryPlan:
    """Never download the whole registry: without a drug / condition / sponsor / country filter,
    restrict to interventional trials started in the last few years."""
    if plan.answer_mode is AnswerMode.UNSUPPORTED:
        return plan
    filters = [merge_filters(plan.shared_filters, c.filters) for c in plan.cohorts] or [plan.shared_filters]
    if any(has_entity_filter(f) for f in filters):
        return plan
    shared = plan.shared_filters.model_dump()
    added = []
    if shared["study_type"] is None:
        shared["study_type"] = StudyType.INTERVENTIONAL
        added.append("interventional studies")
    if shared["start_year"] is None:
        shared["start_year"] = date.today().year - SCOPE_GUARD_YEARS
        added.append(f"started since {shared['start_year']}")
    if not added:
        return plan
    note = (
        "No drug, condition, sponsor or country was given, so the search was limited to "
        + " and ".join(added)
        + ". Add a filter for a more specific answer."
    )
    return plan.model_copy(update={"shared_filters": PlanFilters(**shared), "notes": [*plan.notes, note]})


def _only(**kw) -> PlanFilters:
    return PlanFilters(**{**{name: None for name in PlanFilters.model_fields}, **kw})


def _fmt(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)
