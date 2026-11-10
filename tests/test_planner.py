import pytest

from app.planner.fallback import fallback_plan
from app.planner.llm_client import PlannerError
from app.planner.llm_planner import Planner
from app.planner.prompts import FEW_SHOTS, _f, _plan, build_messages
from app.schemas.enums import DimensionName, VizType
from app.schemas.request import VisualizeRequest
from app.validation import plan_rules
from app.validation.plan_rules import PlanRejected

GOOD = _plan(intent="distribution", viz_type="bar_chart", group_by="phase",
             shared_filters=_f(condition="melanoma"), interpretation="Melanoma trials by phase.")
BAD = _plan(intent="trend", viz_type="time_series", group_by="phase",
            shared_filters=_f(condition="melanoma"), interpretation="x")


class ScriptedLLM:
    model = "fake"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls: list[list[dict]] = []

    def generate_plan(self, messages):
        self.calls.append(messages)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_valid_plan_used_directly():
    llm = ScriptedLLM(GOOD)
    plan = Planner(llm).plan(VisualizeRequest(query="melanoma phases"))
    assert plan.group_by is DimensionName.PHASE and plan.notes == []
    assert len(llm.calls) == 1


def test_invalid_plan_retried_with_violations():
    llm = ScriptedLLM(BAD, GOOD)
    plan = Planner(llm).plan(VisualizeRequest(query="melanoma phases"))
    assert plan.viz_type is VizType.BAR_CHART
    assert "temporal" in llm.calls[1][-1]["content"]  # the violation was fed back
    assert any("corrected" in n for n in plan.notes)


def test_twice_invalid_falls_back():
    plan = Planner(ScriptedLLM(BAD, BAD)).plan(VisualizeRequest(query="melanoma per year", condition="melanoma"))
    assert plan.viz_type is VizType.TIME_SERIES  # keyword rule
    assert plan.shared_filters.condition == "melanoma"  # explicit field still applied
    assert any("failed validation twice" in n for n in plan.notes)


def test_llm_error_falls_back():
    plan = Planner(ScriptedLLM(PlannerError("timeout"))).plan(VisualizeRequest(query="which countries", condition="asthma"))
    assert plan.group_by is DimensionName.COUNTRY
    assert any("unavailable" in n for n in plan.notes)


def test_no_llm_configured_uses_fallback_and_scope_guard():
    plan = Planner(None).plan(VisualizeRequest(query="phase distribution"))
    assert plan.group_by is DimensionName.PHASE
    assert any("not configured" in n for n in plan.notes)
    assert any("limited to" in n for n in plan.notes)


TYPO = _plan(intent="trend", viz_type="time_series", group_by="start_year",
             shared_filters=_f(intervention="pembrolizmab"), interpretation="x")
FIXED = _plan(intent="trend", viz_type="time_series", group_by="start_year",
              shared_filters=_f(intervention="pembrolizumab"), interpretation="x")


def _counter(hits: dict[str, int]):
    """Fake API count: number of trials for a single-term search."""
    calls = []

    def count(params):
        calls.append(params)
        return next((n for term, n in hits.items() if term in str(params)), 0)

    count.calls = calls
    return count


def test_zero_hit_term_is_corrected_and_verified():
    llm = ScriptedLLM(TYPO, FIXED)
    plan = Planner(llm, _counter({"pembrolizumab": 2659})).plan(VisualizeRequest(query="pembrolizmab trials per year"))
    assert plan.shared_filters.intervention == "pembrolizumab"
    assert "match no trials: intervention 'pembrolizmab'" in llm.calls[1][-1]["content"]
    assert any("'pembrolizmab' -> 'pembrolizumab'" in n for n in plan.notes)


def test_correction_rejected_when_api_does_not_confirm_it():
    still_wrong = _plan(intent="trend", viz_type="time_series", group_by="start_year",
                        shared_filters=_f(intervention="pembrolizumabb"), interpretation="x")
    plan = Planner(ScriptedLLM(TYPO, still_wrong), _counter({})).plan(VisualizeRequest(query="q"))
    assert plan.shared_filters.intervention == "pembrolizmab"  # original kept, user told to check spelling
    assert any("match no trials" in n and "Check the spelling" in n for n in plan.notes)


def test_grounding_skipped_when_all_terms_hit():
    llm = ScriptedLLM(GOOD)
    counter = _counter({"melanoma": 3782})
    Planner(llm, counter).plan(VisualizeRequest(query="melanoma phases"))
    assert len(llm.calls) == 1 and len(counter.calls) == 1


def test_explicit_viz_type_that_cannot_work_is_rejected():
    with pytest.raises(PlanRejected, match="temporal"):
        Planner(ScriptedLLM(GOOD)).plan(VisualizeRequest(query="melanoma phases", viz_type="time_series"))


@pytest.mark.parametrize(
    ("query", "viz", "dim"),
    [
        ("trials per year since 2015", VizType.TIME_SERIES, DimensionName.START_YEAR),
        ("which countries are recruiting most", VizType.BAR_CHART, DimensionName.RECRUITING_COUNTRY),
        ("sponsor categories", VizType.BAR_CHART, DimensionName.SPONSOR_CLASS),
        ("network of sponsors and drugs", VizType.NETWORK_GRAPH, None),
        ("anything else", VizType.BAR_CHART, DimensionName.PHASE),
    ],
)
def test_fallback_keywords(query, viz, dim):
    plan = fallback_plan(query)
    assert plan.viz_type is viz and plan.group_by is dim
    assert plan_rules.check(plan) == []


def test_messages_include_few_shots_and_structured_fields():
    messages = build_messages("q", "drug_name=aspirin")
    assert messages[0]["role"] == "system" and "recruiting_country" in messages[0]["content"]
    assert len(messages) == 1 + 2 * len(FEW_SHOTS) + 1
    assert "drug_name=aspirin" in messages[-1]["content"]
