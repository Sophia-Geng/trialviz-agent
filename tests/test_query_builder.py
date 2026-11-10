from app.ctgov.query_builder import build_params, drug_query, has_entity_filter, merge_filters, sanitize
from app.pipeline import empty_filters
from app.schemas.plan import PlanFilters


def f(**kw) -> PlanFilters:
    return PlanFilters(**{**empty_filters().model_dump(), **kw})


def test_drug_query_is_strict_and_quoted():
    assert drug_query("trastuzumab deruxtecan") == (
        '(AREA[InterventionName]"trastuzumab deruxtecan" OR AREA[InterventionMeshTerm]"trastuzumab deruxtecan")'
    )


def test_sanitize_strips_essie_syntax():
    assert sanitize('foo "bar" (baz) [x]') == "foo bar baz x"


def test_build_params_full():
    params = build_params(
        f(
            intervention="pembrolizumab",
            condition="melanoma",
            sponsor="Merck",
            country="France",
            status=["RECRUITING", "COMPLETED"],
            phase=["PHASE2", "PHASE3"],
            start_year=2015,
            end_year=2020,
        )
    )
    assert params == {
        "query.intr": '(AREA[InterventionName]"pembrolizumab" OR AREA[InterventionMeshTerm]"pembrolizumab")',
        "query.cond": "melanoma",
        "query.lead": "Merck",
        "query.locn": "France",
        "filter.overallStatus": "RECRUITING,COMPLETED",
        "filter.advanced": "(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) AND AREA[StartDate]RANGE[2015-01-01,2020-12-31]",
    }


def test_open_ended_year_range():
    assert build_params(f(end_year=2019))["filter.advanced"] == "AREA[StartDate]RANGE[MIN,2019-12-31]"


def test_empty_filters_give_no_params():
    assert build_params(empty_filters()) == {}
    assert not has_entity_filter(empty_filters())


def test_cohort_overrides_shared():
    merged = merge_filters(f(condition="melanoma", start_year=2015), f(intervention="nivolumab", start_year=2020))
    assert (merged.condition, merged.intervention, merged.start_year) == ("melanoma", "nivolumab", 2020)
