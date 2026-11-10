"""POST /visualize response body.

`visualization` is a discriminated union on `type`. Data rows use fixed keys
(`category`, `series`, `trial_count`, ...) so every chart type has a static
schema; `encoding` maps those keys to visual channels and carries the
human-readable axis labels.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.schemas.enums import AnswerMode, DimensionName

CTGOV_API = "https://clinicaltrials.gov/api/v2"

# --- citations ---------------------------------------------------------------


class Citation(BaseModel):
    nct_id: str = Field(examples=["NCT01234567"])
    field: str = Field(description="Path of the supporting field in the API response, with array index.")
    excerpt: str = Field(description="Exact raw value of that field as returned by the API (before normalization).")
    title: str | None = Field(default=None, description="Study brief title, for context only.")
    url: str = Field(examples=["https://clinicaltrials.gov/study/NCT01234567"])


class _Cited(BaseModel):
    citation_count: int = Field(ge=0, description="Total supporting records; `citations` holds at most 5.")
    citations: list[Citation]


# --- encodings ---------------------------------------------------------------

ChannelType = Literal["nominal", "ordinal", "quantitative", "temporal"]


class Channel(BaseModel):
    field: str = Field(description="Key in each data row.")
    type: ChannelType
    label: str = Field(description="Axis / legend title.")


class XYEncoding(BaseModel):
    x: Channel
    y: Channel


class SeriesEncoding(XYEncoding):
    series: Channel


class HistogramEncoding(XYEncoding):
    x2: Channel = Field(description="Bin end; bins are [x, x2).")


class MetricEncoding(BaseModel):
    value: Channel


class NodeEncoding(BaseModel):
    id: Literal["id"] = "id"
    label: Literal["label"] = "label"
    group: Literal["type"] = "type"
    size: Literal["trial_count"] = "trial_count"


class EdgeEncoding(BaseModel):
    source: Literal["source"] = "source"
    target: Literal["target"] = "target"
    weight: Literal["weight"] = "weight"
    directed: Literal[False] = False


class NetworkEncoding(BaseModel):
    nodes: NodeEncoding = NodeEncoding()
    edges: EdgeEncoding = EdgeEncoding()


# --- data rows ---------------------------------------------------------------


class CategoryDatum(_Cited):
    category: str
    trial_count: int = Field(ge=0)


class SeriesDatum(CategoryDatum):
    series: str = Field(description="Cohort or split_by value.")


class TimeDatum(_Cited):
    year: int
    trial_count: int = Field(ge=0)
    estimated_count: int = Field(ge=0, description="Trials in this bucket whose start date is ESTIMATED.")
    incomplete: bool = Field(
        default=False,
        description="Current or future year: counts will still grow, so a drop here is not a trend (draw dashed).",
    )


class BinDatum(_Cited):
    bin_start: float
    bin_end: float
    trial_count: int = Field(ge=0)


class PointDatum(BaseModel):
    nct_id: str
    x: float
    y: float
    label: str
    citations: list[Citation]


class MetricDatum(_Cited):
    value: int = Field(ge=0)
    label: str


class NetworkNode(_Cited):
    id: str = Field(description="'<dimension>:<normalized value>', unique across dimensions.")
    label: str
    type: DimensionName
    trial_count: int = Field(ge=0)
    degree: int = Field(ge=0)


class NetworkEdge(_Cited):
    source: str
    target: str
    weight: int = Field(ge=1, description="Number of distinct trials in which both endpoints appear.")


class NetworkData(BaseModel):
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]


# --- visualizations ----------------------------------------------------------


class _Viz(BaseModel):
    title: str


class BarChart(_Viz):
    type: Literal["bar_chart"] = "bar_chart"
    encoding: XYEncoding
    data: list[CategoryDatum]


class GroupedBarChart(_Viz):
    type: Literal["grouped_bar_chart"] = "grouped_bar_chart"
    encoding: SeriesEncoding
    data: list[SeriesDatum]


class TimeSeries(_Viz):
    type: Literal["time_series"] = "time_series"
    encoding: XYEncoding
    data: list[TimeDatum]


class Histogram(_Viz):
    type: Literal["histogram"] = "histogram"
    encoding: HistogramEncoding
    data: list[BinDatum]


class ScatterPlot(_Viz):
    type: Literal["scatter_plot"] = "scatter_plot"
    encoding: XYEncoding
    data: list[PointDatum]


class Metric(_Viz):
    type: Literal["metric"] = "metric"
    encoding: MetricEncoding
    data: list[MetricDatum] = Field(min_length=1, max_length=1)


class NetworkGraph(_Viz):
    type: Literal["network_graph"] = "network_graph"
    encoding: NetworkEncoding = NetworkEncoding()
    data: NetworkData


Visualization = Annotated[
    BarChart | GroupedBarChart | TimeSeries | Histogram | ScatterPlot | Metric | NetworkGraph,
    Field(discriminator="type"),
]

# --- meta --------------------------------------------------------------------


class SortSpec(BaseModel):
    field: str
    order: Literal["asc", "desc"]


class Grouping(BaseModel):
    group_by: DimensionName | None
    split_by: DimensionName | None = None
    multi_valued: bool = Field(
        description="True if one trial can fall into several buckets, so bucket counts may sum to more than the total."
    )


class Binning(BaseModel):
    method: Literal["linear", "log"]
    bin_count: int = Field(ge=1)


class RecordCounts(BaseModel):
    total_matching: int = Field(ge=0, description="totalCount reported by the API for the filters.")
    fetched: int = Field(ge=0, description="Studies actually downloaded and aggregated.")
    truncated: bool = Field(description="True if fetched < total_matching, i.e. values are based on a sample.")
    count_method: Literal["full_scan", "bucket_count", "api_total"] = Field(
        description="full_scan: counted over fetched records; api_total: the API's exact totalCount (metric)."
    )


class SourceInfo(BaseModel):
    name: Literal["clinicaltrials.gov"] = "clinicaltrials.gov"
    api: str = CTGOV_API
    api_version: str | None = None
    data_timestamp: str | None = None
    retrieved_at: datetime


class Meta(BaseModel):
    answer_mode: AnswerMode
    query_interpretation: str
    filters_applied: dict[str, Any] = Field(description="Final filters sent to the API, per cohort where relevant.")
    plan: dict[str, Any] | None = Field(default=None, description="The validated QueryPlan, for transparency.")
    units: dict[str, str] = Field(default_factory=dict, examples=[{"y": "trials"}])
    sort: SortSpec | None = None
    time_granularity: Literal["year"] | None = None
    grouping: Grouping | None = None
    binning: Binning | None = None
    geo_hint: Literal["country"] | None = Field(default=None, description="Categories are countries; a map is possible.")
    record_counts: RecordCounts | None = None
    notes: list[str] = Field(default_factory=list)
    source: SourceInfo


class VisualizeResponse(BaseModel):
    visualization: Visualization | None = Field(description="Null only when meta.answer_mode is 'unsupported'.")
    meta: Meta


# --- errors ------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: Literal["VALIDATION_ERROR", "PLAN_ERROR", "UPSTREAM_ERROR", "INTERNAL_ERROR"]
    message: str
    details: list[Any] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorBody
