from datetime import datetime, timezone

from app.aggregate.engine import MAX_CITATIONS, co_occurrence, group_by, partition
from app.ctgov.client import FetchResult
from app.pipeline import execute_plan
from app.schemas.enums import DimensionName as D
from app.viz import builder
from tests.conftest import load_fixture, load_studies
from tests.test_query_builder import f

# --- hand-countable mini dataset ------------------------------------------------


def study(nid, phases=None, start=None, start_type="ACTUAL", countries=()):
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nid, "briefTitle": f"Title {nid}"},
            "designModule": {"phases": phases} if phases is not None else {},
            "statusModule": {"startDateStruct": {"date": start, "type": start_type}} if start else {},
            "contactsLocationsModule": {"locations": [{"country": c} for c in countries]},
        }
    }


MINI = [
    study("NCT1", ["PHASE2"], "2016-03", countries=["France", "France", "Spain"]),
    study("NCT2", ["PHASE1", "PHASE2"], "2016-07-01", countries=["France"]),
    study("NCT3", ["PHASE2"], "2018", "ESTIMATED", countries=[]),
    study("NCT4", None, None, countries=["Spain"]),
]


def test_group_by_counts_by_hand():
    r = group_by(MINI, D.PHASE)
    assert {k: b.count for k, b in r.buckets.items()} == {"Phase 2": 2, "Phase 1/2": 1, "Unknown": 1}
    assert sum(b.count for b in r.buckets.values()) == len(MINI)  # single-valued: buckets sum to total


def test_multi_valued_counts_each_trial_once_per_value():
    r = group_by(MINI, D.COUNTRY)
    assert {k: b.count for k, b in r.buckets.items()} == {"france": 2, "spain": 2, "Unknown": 1}
    assert r.buckets["france"].nct_ids == ["NCT1", "NCT2"]


def test_citations_point_to_raw_values():
    r = group_by(MINI, D.START_YEAR)
    (c,) = r.buckets[2018.0].citations
    assert (c.nct_id, c.excerpt, c.field) == ("NCT3", "2018", "protocolSection.statusModule.startDateStruct.date")
    assert c.url == "https://clinicaltrials.gov/study/NCT3"
    assert r.buckets[2018.0].estimated == 1
    assert r.missing == 1


def test_citations_capped():
    studies = load_studies("alzheimer")
    r = group_by(studies, D.PHASE)
    assert all(len(b.citations) == min(b.count, MAX_CITATIONS) for b in r.buckets.values())


def test_time_series_fills_gaps_and_respects_bounds():
    built = builder.time_series(group_by(MINI, D.START_YEAR), "t", start_year=2015, end_year=None)
    rows = [(d.year, d.trial_count, d.estimated_count) for d in built.visualization.data]
    assert rows == [(2015, 0, 0), (2016, 2, 0), (2017, 0, 0), (2018, 1, 1)]
    assert any("start date" in n for n in built.notes)


def test_bar_chart_phase_keeps_natural_order_and_unknown():
    built = builder.bar_chart(group_by(MINI, D.PHASE), "t", top_n=1)
    assert [d.category for d in built.visualization.data] == ["Phase 1/2", "Phase 2", "Unknown"]
    assert built.visualization.encoding.x.type == "ordinal"


def test_bar_chart_ranks_and_truncates_nominal():
    built = builder.bar_chart(group_by(MINI, D.COUNTRY), "t", top_n=1)
    assert [(d.category, d.trial_count) for d in built.visualization.data] == [("France", 2)]
    assert any("top 1 of 2" in n for n in built.notes)
    assert any("not shown" in n for n in built.notes)  # Unknown dropped from rankings
    assert built.grouping.multi_valued


def test_grouped_bar_fills_missing_categories_with_zero():
    a = group_by(MINI[:2], D.PHASE)
    b = group_by(MINI[2:], D.PHASE)
    built = builder.grouped_bar_chart([("A", a), ("B", b)], "t", top_n=15)
    rows = {(d.series, d.category): d.trial_count for d in built.visualization.data}
    assert rows[("B", "Phase 1/2")] == 0 and rows[("A", "Phase 2")] == 1 and rows[("B", "Unknown")] == 1


def _trial(nid, sponsor, drugs):
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nid, "briefTitle": nid},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor}},
            "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": d} for d in drugs]},
        }
    }


NET = [
    _trial("NCT1", "Merck", ["Pembrolizumab", "Placebo"]),
    _trial("NCT2", "Merck", ["Pembrolizumab", "Lenvatinib"]),
    _trial("NCT3", "BMS", ["Nivolumab", "Ipilimumab"]),
    _trial("NCT4", "BMS", ["Nivolumab", "Ipilimumab", "Pembrolizumab"]),
]


def test_co_occurrence_bipartite_weights():
    g = co_occurrence(NET, D.LEAD_SPONSOR, D.DRUG)
    weights = {(e.source, e.target): e.weight for e in g.edges.values()}
    assert weights[("lead_sponsor:merck", "drug:pembrolizumab")] == 2
    assert weights[("lead_sponsor:bms", "drug:ipilimumab")] == 2
    assert ("lead_sponsor:merck", "drug:placebo") not in weights
    edge = g.edges[("lead_sponsor:merck", "drug:lenvatinib")]
    assert edge.citations[0].excerpt == "Merck | Lenvatinib"  # both raw values


def test_co_occurrence_same_dimension_is_undirected():
    g = co_occurrence(NET, D.DRUG, D.DRUG)
    assert g.edges[("drug:ipilimumab", "drug:nivolumab")].weight == 2
    assert all(e.source < e.target for e in g.edges.values())
    assert g.studies_with_edges == 3  # NCT1 has a single drug once placebo is removed


def test_network_pruning():
    g = co_occurrence(NET, D.DRUG, D.DRUG)
    built = builder.network_graph(g, "t", max_nodes=2, min_weight=2)
    data = built.visualization.data
    assert [n.id for n in data.nodes] == ["drug:ipilimumab", "drug:nivolumab"]
    assert [(e.source, e.target, e.weight) for e in data.edges] == [("drug:ipilimumab", "drug:nivolumab", 2)]
    assert data.nodes[0].degree == 1 and data.nodes[0].trial_count == 2
    assert any("fewer than 2" in n for n in built.notes)


def test_partition_by_split_dimension():
    parts = dict(partition(MINI, D.COUNTRY))
    assert [s["protocolSection"]["identificationModule"]["nctId"] for s in parts["France"]] == ["NCT1", "NCT2"]


# --- pipeline with a fake client over recorded fixtures ---------------------------


class FakeClient:
    def __init__(self, fixtures: dict[str, str]):
        self.fixtures = fixtures  # query param value -> fixture name

    def fetch_studies(self, params, fields, max_records):
        name = next(v for k, v in self.fixtures.items() if k in str(params))
        studies = load_studies(name)
        total = load_fixture(name)["pages"][0]["totalCount"]
        return FetchResult(studies[:max_records], total, datetime.now(timezone.utc), 1)

    def version(self):
        return load_fixture("version")["pages"][0]


def test_pipeline_comparison_end_to_end():
    from scripts.run_plan_demo import PLANS

    client = FakeClient({"pembrolizumab": "pembrolizumab_since_2015", "nivolumab": "nivolumab"})
    resp = execute_plan(PLANS["compare"], client)
    assert resp.visualization.type == "grouped_bar_chart"
    assert {d.series for d in resp.visualization.data} == {"Pembrolizumab", "Nivolumab"}
    assert resp.meta.record_counts.truncated  # fixtures hold 100 of ~2000 studies
    assert any("first 100" in n for n in resp.meta.notes)
    assert resp.meta.source.api_version
    # per-series bars sum to the studies fetched for that cohort (phase is single-valued)
    for label in ("Pembrolizumab", "Nivolumab"):
        assert sum(d.trial_count for d in resp.visualization.data if d.series == label) == 100


def test_pipeline_unknown_entity_gives_empty_chart_and_hint():
    class Empty(FakeClient):
        def fetch_studies(self, params, fields, max_records):
            return FetchResult([], 0, datetime.now(timezone.utc), 1)

    from scripts.run_plan_demo import plan

    resp = execute_plan(
        plan(intent="distribution", viz_type="bar_chart", group_by="phase",
             shared_filters=f(intervention="notarealdrugxyz"), interpretation="x"),
        Empty({}),
    )
    assert resp.visualization.data == []
    assert any("No trials matched" in n for n in resp.meta.notes)
