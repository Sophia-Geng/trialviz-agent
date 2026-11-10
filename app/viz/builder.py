"""Aggregation results -> visualization specs (README §4).

Builders return the visualization plus the render hints and notes that belong
in `meta`, so the pipeline only has to assemble them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

from app.aggregate.engine import MAX_CITATIONS, CoOccurrence, GroupResult, extract_batch, make_citation
from app.data.dimensions import DIMENSIONS, Extracted, P, brief_title, nct_id
from app.data.normalize import UNKNOWN, phase_sort_key
from app.schemas.enums import DimensionName
from app.schemas.response import (
    BarChart,
    BinDatum,
    Binning,
    CategoryDatum,
    Channel,
    GroupedBarChart,
    Grouping,
    Histogram,
    HistogramEncoding,
    Metric,
    MetricDatum,
    MetricEncoding,
    NetworkData,
    NetworkEdge,
    NetworkGraph,
    NetworkNode,
    PointDatum,
    ScatterPlot,
    SeriesDatum,
    SeriesEncoding,
    SortSpec,
    TimeDatum,
    TimeSeries,
    XYEncoding,
)

TRIALS = Channel(field="trial_count", type="quantitative", label="Trials")
IDENTIFICATION = f"{P}.identificationModule"
ORDINAL_DIMENSIONS = {DimensionName.PHASE}
# Long-tailed counts: 1-2-5 log bins (0, 1, 2, 5, 10, 20, 50, ...) instead of equal widths.
LOG_BINNED = {DimensionName.ENROLLMENT, DimensionName.SITE_COUNT}
LINEAR_TARGET_BINS = 15
MAX_SCATTER_POINTS = 2000
EDGES_PER_NODE = 3


@dataclass
class Built:
    visualization: BarChart | GroupedBarChart | TimeSeries | Metric | NetworkGraph | Histogram | ScatterPlot
    sort: SortSpec | None = None
    grouping: Grouping | None = None
    binning: Binning | None = None
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


def network_graph(
    graph: CoOccurrence, title: str, max_nodes: int, min_weight: int = 2, edges_per_node: int = EDGES_PER_NODE
) -> Built:
    """Readable network in three steps:
    1. drop edges with weight < min_weight (falls back to 1 for sparse graphs);
    2. keep the max_nodes nodes with the highest weighted degree;
    3. backbone: keep each kept node's `edges_per_node` strongest edges (union), so dense
       clusters do not turn into a hairball while every node still shows its main links.
    Node trial_count / degree describe the full data; only the drawn edges are pruned."""
    notes: list[str] = []
    edges = [e for e in graph.edges.values() if e.weight >= min_weight]
    if not edges and min_weight > 1 and graph.edges:
        edges = list(graph.edges.values())
        notes.append("Few shared trials: edges with weight 1 are included.")
        min_weight = 1
    dropped_weak = len(graph.edges) - len(edges)

    strength: dict[str, int] = {}
    for e in edges:
        strength[e.source] = strength.get(e.source, 0) + e.weight
        strength[e.target] = strength.get(e.target, 0) + e.weight
    ranked = sorted(strength, key=lambda nid: (-strength[nid], graph.nodes[nid].label))
    kept = set(ranked[:max_nodes])
    edges = [e for e in edges if e.source in kept and e.target in kept]

    candidate_count = len(edges)
    by_node: dict[str, list] = {}
    for e in edges:
        by_node.setdefault(e.source, []).append(e)
        by_node.setdefault(e.target, []).append(e)
    backbone = {
        (e.source, e.target)
        for node_edges in by_node.values()
        for e in sorted(node_edges, key=lambda e: -e.weight)[:edges_per_node]
    }
    edges = [e for e in edges if (e.source, e.target) in backbone]
    if len(edges) < candidate_count:
        notes.append(
            f"For readability each entity keeps its {edges_per_node} strongest links "
            f"({len(edges)} of {candidate_count} links among the shown entities)."
        )
    degree: dict[str, int] = {}
    for e in edges:
        degree[e.source] = degree.get(e.source, 0) + 1
        degree[e.target] = degree.get(e.target, 0) + 1
    node_ids = [nid for nid in ranked if nid in degree]

    if dropped_weak:
        notes.append(f"{dropped_weak} edges supported by fewer than {min_weight} trials were removed.")
    if len(strength) > len(node_ids):
        notes.append(f"Showing the {len(node_ids)} most connected of {len(strength)} entities.")
    notes.append(
        f"Edge weight = number of distinct trials in which both entities appear "
        f"({graph.studies_with_edges} trials contributed at least one edge)."
    )

    nodes = [
        NetworkNode(
            id=nid,
            label=graph.nodes[nid].label,
            type=graph.nodes[nid].dimension,
            trial_count=len(graph.nodes[nid].nct_ids),
            degree=degree[nid],
            citation_count=len(graph.nodes[nid].nct_ids),
            citations=graph.nodes[nid].citations,
        )
        for nid in node_ids
    ]
    edge_rows = [
        NetworkEdge(source=e.source, target=e.target, weight=e.weight, citation_count=e.weight, citations=e.citations)
        for e in sorted(edges, key=lambda e: -e.weight)
    ]
    return Built(
        visualization=NetworkGraph(title=title, data=NetworkData(nodes=nodes, edges=edge_rows)),
        units={"node_size": "trials", "edge_weight": "shared trials"},
        sort=SortSpec(field="weight", order="desc"),
        notes=notes,
    )


def log_edges(max_value: float) -> list[float]:
    """0, 1, 2, 5, 10, 20, 50, ... up to the first edge above max_value."""
    edges = [0.0, 1.0]
    while edges[-1] <= max_value:
        last = edges[-1]
        mag = 10 ** math.floor(math.log10(last))
        step = last / mag
        edges.append((2 if step == 1 else 5 if step == 2 else 10) * mag)
    return edges


def linear_edges(lo: float, hi: float, target_bins: int = LINEAR_TARGET_BINS) -> list[float]:
    """Equal-width bins with a 'nice' width (1, 2 or 5 x 10^k)."""
    raw = max((hi - lo) / target_bins, 1e-9)
    mag = 10 ** math.floor(math.log10(raw))
    width = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    start = math.floor(lo / width) * width
    edges = [start]
    while edges[-1] <= hi:
        edges.append(round(edges[-1] + width, 10))
    return edges


def histogram(studies: list[dict], dim_name: DimensionName, title: str) -> Built:
    dim = DIMENSIONS[dim_name]
    pairs = [(s, vals[0]) for s, vals in zip(studies, extract_batch(studies, dim_name), strict=True) if vals]
    notes: list[str] = []
    if len(pairs) < len(studies):
        notes.append(f"{len(studies) - len(pairs)} trials without a value for {dim.label.lower()} are not shown.")
    values = [float(v.key) for _, v in pairs]

    log = dim_name in LOG_BINNED
    edges = (log_edges(max(values)) if log else linear_edges(min(values), max(values))) if values else []
    rows: list[BinDatum] = []
    for lo, hi in pairwise(edges):
        members = [(s, v) for s, v in pairs if lo <= float(v.key) < hi]
        rows.append(
            BinDatum(
                bin_start=lo,
                bin_end=hi,
                trial_count=len(members),
                citation_count=len(members),
                citations=[make_citation(s, v) for s, v in members[:MAX_CITATIONS]],
            )
        )
    if log:
        notes.append("Bins grow on a 1-2-5 scale (0, 1, 2, 5, 10, 20, ...) because the values are long-tailed.")
    estimated = sum(v.estimated for _, v in pairs)
    if estimated:
        what = "planned enrollment" if dim_name is DimensionName.ENROLLMENT else "ESTIMATED dates"
        notes.append(
            f"{estimated} of {len(pairs)} values ({estimated / len(pairs):.0%}) are {what} reported by trials "
            "that are not yet completed, not actual figures."
        )
    zero = sum(1 for _, v in pairs if float(v.key) == 0)
    if zero and dim_name is DimensionName.ENROLLMENT:
        notes.append(f"{zero} trials report 0 participants (typically withdrawn before enrolling).")
    return Built(
        visualization=Histogram(
            title=title,
            encoding=HistogramEncoding(
                x=Channel(field="bin_start", type="quantitative", label=dim.label),
                x2=Channel(field="bin_end", type="quantitative", label=dim.label),
                y=TRIALS,
            ),
            data=rows,
        ),
        sort=SortSpec(field="bin_start", order="asc"),
        binning=Binning(method="log" if log else "linear", bin_count=len(rows)),
        units={"x": dim.label.lower(), "y": "trials"},
        notes=notes,
    )


def scatter_plot(studies: list[dict], x_dim: DimensionName, y_dim: DimensionName, title: str) -> Built:
    xs = extract_batch(studies, x_dim)
    ys = extract_batch(studies, y_dim)
    points = []
    for study, xv, yv in zip(studies, xs, ys, strict=True):
        if xv and yv:
            points.append(
                PointDatum(
                    nct_id=nct_id(study),
                    x=float(xv[0].key),
                    y=float(yv[0].key),
                    label=brief_title(study) or nct_id(study),
                    citations=[make_citation(study, xv[0]), make_citation(study, yv[0])],
                )
            )
    notes = []
    if len(points) < len(studies):
        notes.append(f"{len(studies) - len(points)} trials missing either value are not shown.")
    if len(points) > MAX_SCATTER_POINTS:
        notes.append(f"Showing the first {MAX_SCATTER_POINTS} of {len(points)} trials (API order).")
        points = points[:MAX_SCATTER_POINTS]
    x_label, y_label = DIMENSIONS[x_dim].label, DIMENSIONS[y_dim].label
    if {x_dim, y_dim} & LOG_BINNED:
        notes.append(
            "Values are long-tailed; a log scale is recommended for "
            + " and ".join(DIMENSIONS[d].label.lower() for d in (x_dim, y_dim) if d in LOG_BINNED)
            + "."
        )
    return Built(
        visualization=ScatterPlot(
            title=title,
            encoding=XYEncoding(
                x=Channel(field="x", type="quantitative", label=x_label),
                y=Channel(field="y", type="quantitative", label=y_label),
            ),
            data=points,
        ),
        units={"x": x_label.lower(), "y": y_label.lower()},
        notes=notes,
    )


def metric(total: int, studies: list[dict], title: str, label: str) -> Built:
    citations = [
        make_citation(s, Extracted(nct_id(s), nct_id(s), brief_title(s) or nct_id(s), f"{IDENTIFICATION}.briefTitle"))
        for s in studies[:MAX_CITATIONS]
    ]
    return Built(
        visualization=Metric(
            title=title,
            encoding=MetricEncoding(value=Channel(field="value", type="quantitative", label="Trials")),
            data=[MetricDatum(value=total, label=label, citation_count=total, citations=citations)],
        ),
        notes=["Exact count reported by ClinicalTrials.gov for these filters; citations show example matching trials."],
    )


def time_series(
    result: GroupResult,
    title: str,
    start_year: int | None = None,
    end_year: int | None = None,
    today: date | None = None,
) -> Built:
    current_year = (today or date.today()).year
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
                    incomplete=year >= current_year,
                    citation_count=b.count if b else 0,
                    citations=b.citations if b else [],
                )
            )
    if result.missing:
        notes.append(f"{result.missing} trials without a parseable start date are not shown.")
    if any(d.estimated_count for d in data):
        notes.append("Some trials have ESTIMATED (planned) start dates, see estimated_count per year.")
    incomplete = [d.year for d in data if d.incomplete]
    if incomplete:
        span = str(incomplete[0]) if len(incomplete) == 1 else f"{incomplete[0]}–{incomplete[-1]}"
        notes.append(
            f"{span}: the current year is not over and later years only contain trials that have already "
            f"registered a planned start, so a drop there is not a real decline (marked incomplete)."
        )
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
