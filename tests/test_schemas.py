from datetime import datetime, timezone

import pytest
from openai.lib._pydantic import to_strict_json_schema  # same conversion the SDK applies in .parse()
from pydantic import TypeAdapter, ValidationError

from app.schemas.enums import OverallStatus, Phase
from app.schemas.plan import QueryPlanLLM
from app.schemas.request import VisualizeRequest
from app.schemas.response import VisualizeResponse
from tests.conftest import load_fixture

# --- request -----------------------------------------------------------------


def test_minimal_request_gets_defaults():
    r = VisualizeRequest(query="  How many trials per phase?  ")
    assert r.query == "How many trials per phase?"
    assert (r.max_records, r.top_n) == (10_000, 15)
    assert r.drug_names == [] and r.conditions == []


def test_comparison_lists():
    r = VisualizeRequest(query="compare", drug_name=["Pembrolizumab", "Nivolumab"])
    assert r.drug_names == ["Pembrolizumab", "Nivolumab"]


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "x" * 501},
        {"query": "q", "drug_name": ["a", "b", "c", "d", "e"]},
        {"query": "q", "drug_name": ["Aspirin", "aspirin"]},
        {"query": "q", "drug_name": ["a", "b"], "condition": ["c", "d"]},
        {"query": "q", "start_year": 2020, "end_year": 2010},
        {"query": "q", "start_year": 1950},
        {"query": "q", "trial_phase": ["PHASE5"]},
        {"query": "q", "max_records": 0},
        {"query": "q", "top_n": 100},
        {"query": "q", "unknown_field": 1},
    ],
)
def test_invalid_requests_rejected(payload):
    with pytest.raises(ValidationError):
        VisualizeRequest(**payload)


# --- enums stay in sync with the live API --------------------------------------


def _api_enum(type_name: str) -> set[str]:
    enums = load_fixture("enums")["pages"][0]
    (entry,) = [e for e in enums if e["type"] == type_name]
    return {v["value"] for v in entry["values"]}


def test_phase_enum_matches_api():
    assert {p.value for p in Phase} == _api_enum("Phase")


def test_status_enum_matches_api():
    assert {s.value for s in OverallStatus} == _api_enum("Status")


# --- plan --------------------------------------------------------------------


def test_plan_schema_is_valid_openai_strict_schema():
    schema = to_strict_json_schema(QueryPlanLLM)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


# --- response ----------------------------------------------------------------

_META = {
    "answer_mode": "visualization",
    "query_interpretation": "Trials per phase",
    "filters_applied": {"condition": "melanoma"},
    "source": {"retrieved_at": datetime.now(timezone.utc).isoformat()},
}


def test_response_discriminates_on_type():
    resp = VisualizeResponse.model_validate(
        {
            "visualization": {
                "type": "bar_chart",
                "title": "Trials by phase",
                "encoding": {
                    "x": {"field": "category", "type": "nominal", "label": "Phase"},
                    "y": {"field": "trial_count", "type": "quantitative", "label": "Trials"},
                },
                "data": [{"category": "Phase 2", "trial_count": 78, "citation_count": 78, "citations": []}],
            },
            "meta": _META,
        }
    )
    assert type(resp.visualization).__name__ == "BarChart"


def test_network_response_and_unsupported():
    net = VisualizeResponse.model_validate(
        {
            "visualization": {
                "type": "network_graph",
                "title": "Sponsors and drugs",
                "data": {
                    "nodes": [
                        {"id": "drug:pembrolizumab", "label": "pembrolizumab", "type": "drug", "trial_count": 3,
                         "degree": 1, "citation_count": 3, "citations": []},
                    ],
                    "edges": [],
                },
            },
            "meta": _META,
        }
    )
    assert net.visualization.encoding.edges.directed is False
    unsupported = VisualizeResponse.model_validate({"visualization": None, "meta": {**_META, "answer_mode": "unsupported"}})
    assert unsupported.visualization is None


def test_response_json_schema_exports():
    schema = TypeAdapter(VisualizeResponse).json_schema()
    assert "BarChart" in schema["$defs"] and "NetworkGraph" in schema["$defs"]
