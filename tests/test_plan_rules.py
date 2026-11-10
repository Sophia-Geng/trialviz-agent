import pytest

from app.planner.prompts import FEW_SHOTS, _f, _plan
from app.schemas.enums import StudyType, VizType
from app.schemas.plan import Cohort, NetworkSpec
from app.schemas.request import VisualizeRequest
from app.validation import plan_rules


@pytest.mark.parametrize(("question", "plan"), FEW_SHOTS, ids=[q[:40] for q, _ in FEW_SHOTS])
def test_few_shot_plans_pass_rules(question, plan):
    assert plan_rules.check(plan) == []


@pytest.mark.parametrize(
    ("plan", "fragment"),
    [
        (_plan(intent="trend", viz_type="time_series", group_by="phase", interpretation="x"), "temporal"),
        (_plan(intent="distribution", viz_type="bar_chart", group_by="start_year", interpretation="x"), "categorical"),
        (
            _plan(intent="distribution", viz_type="grouped_bar_chart", group_by="phase", interpretation="x"),
            "2+ cohorts",
        ),
        (_plan(intent="network", viz_type="network_graph", interpretation="x"), "network_graph requires"),
        (
            _plan(
                intent="network",
                viz_type="network_graph",
                interpretation="x",
                network=NetworkSpec(source_dim="enrollment", target_dim="drug"),
            ),
            "categorical",
        ),
        (_plan(intent="distribution", viz_type="histogram", interpretation="x"), "numeric"),
        (_plan(intent="count", viz_type="metric", interpretation="x"), "single_value"),
        (_plan(answer_mode="unsupported", intent="count", viz_type=None, interpretation="x"), "unsupported_reason"),
        (
            _plan(
                intent="comparison",
                viz_type="grouped_bar_chart",
                group_by="phase",
                interpretation="x",
                cohorts=[Cohort(label="A", filters=_f(intervention="a"))],
            ),
            "single cohort",
        ),
        (_plan(intent="distribution", viz_type="bar_chart", group_by="phase", top_n=500, interpretation="x"), "top_n"),
    ],
)
def test_violations_are_reported(plan, fragment):
    errors = plan_rules.check(plan)
    assert any(fragment in e for e in errors), errors


def _internal(plan, **request):
    req = VisualizeRequest(query="q", **request)
    return plan_rules.apply_request(plan_rules.to_internal(plan, req), req)


def test_explicit_fields_win_and_conflicts_are_noted():
    plan = _plan(
        intent="distribution",
        viz_type="bar_chart",
        group_by="phase",
        shared_filters=_f(intervention="keytruda"),
        interpretation="x",
    )
    result = _internal(plan, drug_name="pembrolizumab", start_year=2018)
    assert result.shared_filters.intervention == "pembrolizumab"
    assert result.shared_filters.start_year == 2018
    assert any("instead of 'keytruda'" in n for n in result.notes)


def test_same_value_different_case_is_not_a_conflict():
    plan = _plan(
        intent="distribution",
        viz_type="bar_chart",
        group_by="phase",
        shared_filters=_f(intervention="Pembrolizumab"),
        interpretation="x",
    )
    assert _internal(plan, drug_name="pembrolizumab").notes == []


def test_drug_list_turns_bar_into_comparison():
    plan = _plan(intent="distribution", viz_type="bar_chart", group_by="phase", interpretation="x")
    result = _internal(plan, drug_name=["pembrolizumab", "nivolumab"])
    assert result.viz_type is VizType.GROUPED_BAR_CHART
    assert [c.label for c in result.cohorts] == ["pembrolizumab", "nivolumab"]
    assert plan_rules.check(result) == []


def test_top_n_from_question_kept_unless_request_sets_it():
    plan = _plan(intent="geographic", viz_type="bar_chart", group_by="country", top_n=10, interpretation="x")
    assert _internal(plan).top_n == 10
    assert _internal(plan, top_n=5).top_n == 5
    assert (
        _internal(_plan(intent="geographic", viz_type="bar_chart", group_by="country", interpretation="x")).top_n == 15
    )


def test_scope_guard_limits_unfiltered_queries():
    plan = _internal(
        _plan(
            intent="network",
            viz_type="network_graph",
            interpretation="x",
            network=NetworkSpec(source_dim="drug", target_dim="drug"),
        )
    )
    guarded = plan_rules.apply_scope_guard(plan)
    assert guarded.shared_filters.study_type is StudyType.INTERVENTIONAL
    assert guarded.shared_filters.start_year is not None
    assert any("limited to" in n for n in guarded.notes)


def test_scope_guard_leaves_filtered_queries_alone():
    plan = _internal(
        _plan(
            intent="distribution",
            viz_type="bar_chart",
            group_by="phase",
            shared_filters=_f(condition="melanoma"),
            interpretation="x",
        )
    )
    assert plan_rules.apply_scope_guard(plan) == plan
