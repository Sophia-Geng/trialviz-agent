import pytest
from fastapi.testclient import TestClient

from app.ctgov.client import UpstreamError
from app.main import app
from app.planner.llm_planner import Planner
from app.planner.prompts import _f, _plan
from tests.test_aggregate import FakeClient
from tests.test_planner import ScriptedLLM

PHASES = _plan(intent="distribution", viz_type="bar_chart", group_by="phase",
               shared_filters=_f(condition="melanoma"), interpretation="Melanoma trials by phase.")


@pytest.fixture
def client():
    app.state.ctgov = FakeClient({"melanoma": "melanoma"})
    app.state.planner = Planner(ScriptedLLM(PHASES))
    return TestClient(app)


def test_visualize_returns_spec_with_citations(client):
    resp = client.post("/visualize", json={"query": "How are melanoma trials distributed across phases?"})
    assert resp.status_code == 200
    body = resp.json()
    viz = body["visualization"]
    assert viz["type"] == "bar_chart" and viz["encoding"]["x"]["label"] == "Phase"
    assert sum(d["trial_count"] for d in viz["data"]) == 100
    c = viz["data"][0]["citations"][0]
    assert c["url"].startswith("https://clinicaltrials.gov/study/NCT") and c["excerpt"]
    assert body["meta"]["filters_applied"]["api_params"] == {"query.cond": "melanoma"}


def test_invalid_request_uses_error_schema(client):
    resp = client.post("/visualize", json={"query": "", "top_n": 999})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR" and len(err["details"]) == 2


def test_impossible_explicit_viz_type(client):
    resp = client.post("/visualize", json={"query": "melanoma phases", "viz_type": "time_series"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "PLAN_ERROR"


def test_upstream_failure_is_502(client):
    class Down(FakeClient):
        def fetch_studies(self, *a, **kw):
            raise UpstreamError("ClinicalTrials.gov unavailable")

    app.state.ctgov = Down({})
    resp = client.post("/visualize", json={"query": "melanoma phases"})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_schema_endpoint(client):
    body = client.get("/schema").json()
    assert set(body) == {"request", "response", "error"}
    assert "query" in body["request"]["properties"]
