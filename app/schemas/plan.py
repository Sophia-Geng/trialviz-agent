"""QueryPlan: the only thing the LLM produces.

`QueryPlanLLM` is sent to OpenAI as a strict Structured Outputs schema, so it
follows strict-mode rules: no free-form dicts, every field required (optional
ones are `X | None`), no defaults, no extra keys. Numeric bounds are enforced
later by validation/plan_rules.py rather than in the schema.

`QueryPlan` is the internal, validated version that the rest of the pipeline
consumes; it adds request-level settings and planner notes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import AnswerMode, DimensionName, Intent, OverallStatus, Phase, StudyType, VizType


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanFilters(_Strict):
    """Search filters. Entity fields hold strings copied from the question; never numbers or results."""

    intervention: str | None = Field(description="Drug / intervention name, e.g. 'pembrolizumab'.")
    condition: str | None = Field(description="Condition / disease, e.g. 'melanoma'.")
    sponsor: str | None = Field(description="Lead sponsor name.")
    country: str | None = Field(description="Country of trial sites.")
    status: list[OverallStatus] | None = Field(description="Overall recruitment status filter.")
    phase: list[Phase] | None = Field(description="Phase filter.")
    study_type: StudyType | None = Field(description="Interventional / observational / expanded access.")
    start_year: int | None = Field(description="Trials starting in or after this year.")
    end_year: int | None = Field(description="Trials starting in or before this year.")


class Cohort(_Strict):
    label: str = Field(description="Series label shown in the chart, e.g. 'Pembrolizumab'.")
    filters: PlanFilters = Field(description="Filters that define this cohort (combined with shared_filters).")


class MeasureSpec(_Strict):
    x: DimensionName = Field(description="Numeric dimension (histogram) or x-axis (scatter).")
    y: DimensionName | None = Field(description="Second numeric dimension for scatter plots; null otherwise.")


class NetworkSpec(_Strict):
    source_dim: DimensionName = Field(description="Categorical dimension for one end of each edge.")
    target_dim: DimensionName = Field(
        description="Categorical dimension for the other end. Same as source_dim for co-occurrence networks."
    )


class QueryPlanLLM(_Strict):
    answer_mode: AnswerMode = Field(
        description="visualization: answer with a chart; single_value: the question asks for one number; "
        "unsupported: not answerable from ClinicalTrials.gov registry fields."
    )
    unsupported_reason: str | None = Field(description="Why the question is unsupported; null otherwise.")
    intent: Intent
    cohorts: list[Cohort] = Field(
        description="One entry per compared group (e.g. Drug A vs Drug B). Empty when there is no comparison."
    )
    shared_filters: PlanFilters = Field(description="Filters applied to every cohort.")
    group_by: DimensionName | None = Field(description="Dimension on the x-axis / categories.")
    split_by: DimensionName | None = Field(description="Optional second categorical dimension (series).")
    measure: MeasureSpec | None = Field(description="Numeric dimensions for histogram / scatter_plot.")
    network: NetworkSpec | None = Field(description="Endpoints for network_graph; null otherwise.")
    viz_type: VizType | None = Field(description="Chosen visualization; null when unsupported.")
    top_n: int | None = Field(description="Max categories / nodes, if the question implies one (e.g. 'top 10').")
    interpretation: str = Field(description="One sentence restating what will be counted.")


class QueryPlan(QueryPlanLLM):
    """Validated plan after merging explicit request fields and applying defaults."""

    top_n: int = 15
    max_records: int = 10_000
    notes: list[str] = Field(default_factory=list, description="Assumptions and adjustments, surfaced in meta.notes.")
