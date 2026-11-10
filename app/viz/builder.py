"""Aggregation results -> visualization specs (PLAN.md §8).

Builders return the visualization plus the render hints and notes that belong
in `meta`, so the pipeline only has to assemble them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.aggregate.engine import GroupResult
from app.data.normalize import UNKNOWN, phase_sort_key
from app.schemas.enums import DimensionName
from app.schemas.response import (
    BarChart,
    CategoryDatum,
    Channel,
    GroupedBarChart,
    Grouping,
    SeriesDatum,
    SeriesEncoding,
    SortSpec,
    TimeDatum,
    TimeSeries,
    XYEncoding,
)

TRIALS = Channel(field="trial_count", type="quantitative", label="Trials")
ORDINAL_DIMENSIONS = {DimensionName.PHASE}


@dataclass
class Built:
    visualization: BarChart | GroupedBarChart | TimeSeries
    sort: SortSpec | None = None
    grouping: Grouping | None = None
    time_granularity: str | None = None
    units: dict[str, str] = field(default_factory=lambda: {"y": "trials"})
    notes: list[str] = field(default_factory=list)


def _multi_valued_note(result: GroupResult) -> list[str]:
    if not result.dimension.multi_valued:
        return []
    return [
        f"A trial can have several {result.dimension.label.lower()} values and is counted once in each, "
        "so bars may sum to more than the number of trials."
    ]


def _ordered_keys(result: GroupResult, totals: dict, top_n: int) -> tuple[list, list[str]]:
    """Category order and notes. Ordinal dimensions keep every category in their natural order;
    others are ranked by count, Unknown is dropped and the list is cut to top_n."""
    notes: list[str] = []
    name = result.dimension.name
    keys = list(totals)
    if name in ORDINAL_DIMENSIONS:
        return sorted(keys, key=lambda k: phase_sort_key(str(k))), notes

    labels = {k: b.label for k, b in result.buckets.items()}
    if UNKNOWN in totals:
        notes.append(f"{totals[UNKNOWN]} trials with no {result.dimension.label.lower()} recorded are not shown.")
        keys.remove(UNKNOWN)
    keys.sort(key=lambda k: (-totals[k], labels.get(k, str(k))))
    if len(keys) > top_n:
        notes.append(f"Showing the top {top_n} of {len(keys)} {result.dimension.label.lower()} values.")
        keys = keys[:top_n]
    return keys, notes


def bar_chart(result: GroupResult, title: str, top_n: int) -> Built:
    totals = {k: b.count for k, b in result.buckets.items()}
    keys, notes = _ordered_keys(result, totals, top_n)
    data = [
        CategoryDatum(
            category=result.buckets[k].label,
            trial_count=result.buckets[k].count,
            citation_count=result.buckets[k].count,
            citations=result.buckets[k].citations,
        )
        for k in keys
    ]
    ordinal = result.dimension.name in ORDINAL_DIMENSIONS
    return Built(
        visualization=BarChart(
            title=title,
            encoding=XYEncoding(
                x=Channel(field="category", type="ordinal" if ordinal else "nominal", label=result.dimension.label),
                y=TRIALS,
            ),
            data=data,
        ),
        sort=SortSpec(field="category", order="asc") if ordinal else SortSpec(field="trial_count", order="desc"),
        grouping=Grouping(group_by=result.dimension.name, multi_valued=result.dimension.multi_valued),
        notes=notes + _multi_valued_note(result),
    )


def grouped_bar_chart(
    series: list[tuple[str, GroupResult]], title: str, top_n: int, split_by: DimensionName | None = None
) -> Built:
    """One GroupResult per series (cohort or split_by value), sharing the same x dimension."""
    first = series[0][1]
    totals: dict = {}
    labels: dict = {}
    for _, result in series:
        for k, b in result.buckets.items():
            totals[k] = totals.get(k, 0) + b.count
            labels.setdefault(k, b.label)
    keys, notes = _ordered_keys(first, totals, top_n)

    data = []
    for series_label, result in series:
        for k in keys:
            bucket = result.buckets.get(k)
            data.append(
                SeriesDatum(
                    category=labels[k],
                    series=series_label,
                    trial_count=bucket.count if bucket else 0,
                    citation_count=bucket.count if bucket else 0,
                    citations=bucket.citations if bucket else [],
                )
            )
    ordinal = first.dimension.name in ORDINAL_DIMENSIONS
    series_label_text = "Cohort" if split_by is None else first.dimension.label
    return Built(
        visualization=GroupedBarChart(
            title=title,
            encoding=SeriesEncoding(
                x=Channel(field="category", type="ordinal" if ordinal else "nominal", label=first.dimension.label),
                y=TRIALS,
                series=Channel(field="series", type="nominal", label=series_label_text),
            ),
            data=data,
        ),
        sort=SortSpec(field="category", order="asc") if ordinal else SortSpec(field="trial_count", order="desc"),
        grouping=Grouping(group_by=first.dimension.name, split_by=split_by, multi_valued=first.dimension.multi_valued),
        notes=notes + _multi_valued_note(first),
    )


def time_series(result: GroupResult, title: str, start_year: int | None = None, end_year: int | None = None) -> Built:
    years = {int(k): b for k, b in result.buckets.items()}
    notes: list[str] = []
    if not years:
        lo = hi = None
    else:
        lo = start_year or min(years)
        hi = end_year or max(years)
    data = []
    if lo is not None:
        for year in range(lo, hi + 1):
            b = years.get(year)
            data.append(
                TimeDatum(
                    year=year,
                    trial_count=b.count if b else 0,
                    estimated_count=b.estimated if b else 0,
                    citation_count=b.count if b else 0,
                    citations=b.citations if b else [],
                )
            )
    if result.missing:
        notes.append(f"{result.missing} trials without a parseable start date are not shown.")
    if any(d.estimated_count for d in data):
        notes.append("Recent and future years include trials with ESTIMATED start dates (see estimated_count).")
    return Built(
        visualization=TimeSeries(
            title=title,
            encoding=XYEncoding(x=Channel(field="year", type="temporal", label=result.dimension.label), y=TRIALS),
            data=data,
        ),
        sort=SortSpec(field="year", order="asc"),
        grouping=Grouping(group_by=result.dimension.name, multi_valued=False),
        time_granularity="year",
        notes=notes,
    )
