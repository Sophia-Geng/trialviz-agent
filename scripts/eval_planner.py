"""Planner evaluation: labeled questions -> full planner (LLM + rules + retry + fallback) per model.

Each case states what a correct plan must contain; a case passes only if every stated check holds.
Unstated fields are free (e.g. a correct phase question may or may not set top_n).

Usage:
    python scripts/eval_planner.py                       # default models
    python scripts/eval_planner.py gpt-4.1 gpt-4.1-mini  # chosen models

Writes docs/planner_eval.md (summary + per-case matrix) and docs/planner_eval.json (raw plans).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ctgov.client import CtgovClient  # noqa: E402
from app.planner.llm_client import OpenAIPlanner  # noqa: E402
from app.planner.llm_planner import ENTITY_FIELDS, _only  # noqa: E402
from app.ctgov.query_builder import build_params  # noqa: E402
from app.planner.llm_planner import Planner  # noqa: E402
from app.schemas.plan import QueryPlan  # noqa: E402
from app.schemas.request import VisualizeRequest  # noqa: E402
from app.validation.plan_rules import PlanRejected  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"
DEFAULT_MODELS = ["gpt-4.1", "gpt-4.1-mini", "gpt-5.4-mini"]

# expect keys: answer_mode, viz_type, group_by (str | set of acceptable), split_by, network (unordered pair),
# measure (x, y), entity {field: substring}, status / phase (sets), start_year, top_n, cohorts (count)
CASES: list[dict] = [
    # --- assignment appendix ---------------------------------------------------------------------
    {"id": "trend-drug", "tag": "appendix", "query": "How has the number of trials for pembrolizumab changed per year since 2015?",
     "expect": {"viz_type": "time_series", "group_by": "start_year", "entity": {"intervention": "pembrolizumab"}, "start_year": 2015}},
    {"id": "trend-condition", "tag": "appendix", "query": "How many trials started each year for type 2 diabetes?",
     "expect": {"viz_type": "time_series", "group_by": "start_year", "entity": {"condition": "diabetes"}}},
    {"id": "phase-distribution", "tag": "appendix", "query": "How are Alzheimer's disease trials distributed across phases?",
     "expect": {"viz_type": "bar_chart", "group_by": "phase", "entity": {"condition": "alzheimer"}}},
    {"id": "intervention-types", "tag": "appendix", "query": "What are the most common intervention types for Parkinson's disease trials?",
     "expect": {"viz_type": "bar_chart", "group_by": "intervention_type", "entity": {"condition": "parkinson"}}},
    {"id": "compare-drugs", "tag": "appendix", "query": "Compare phases for trials involving atezolizumab vs durvalumab.",
     "expect": {"viz_type": "grouped_bar_chart", "group_by": "phase", "cohorts": 2}},
    {"id": "compare-sponsor-class", "tag": "appendix", "query": "Compare sponsor categories across lung cancer and colorectal cancer trials.",
     "expect": {"viz_type": "grouped_bar_chart", "group_by": "sponsor_class", "cohorts": 2}},
    {"id": "recruiting-countries", "tag": "appendix", "query": "Which countries have the most recruiting trials for asthma?",
     "expect": {"viz_type": "bar_chart", "group_by": "recruiting_country", "entity": {"condition": "asthma"}, "status": {"RECRUITING"}}},
    {"id": "network-sponsor-drug", "tag": "appendix", "query": "Show a network of sponsors and drugs for glioblastoma trials.",
     "expect": {"viz_type": "network_graph", "network": ("lead_sponsor", "drug"), "entity": {"condition": "glioblastoma"}}},
    {"id": "network-drug-drug", "tag": "appendix", "query": "Which drugs frequently co-occur in combination studies for multiple myeloma?",
     "expect": {"viz_type": "network_graph", "network": ("drug", "drug"), "entity": {"condition": "myeloma"}}},
    # --- other chart types -----------------------------------------------------------------------
    {"id": "histogram", "tag": "chart types", "query": "What is the distribution of enrollment sizes in phase 2 psoriasis trials?",
     "expect": {"viz_type": "histogram", "measure": ("enrollment", None), "phase": {"PHASE2"}, "entity": {"condition": "psoriasis"}}},
    {"id": "scatter", "tag": "chart types", "query": "Plot enrollment against trial duration for completed asthma trials.",
     "expect": {"viz_type": "scatter_plot", "measure": ("enrollment", "duration_months"), "status": {"COMPLETED"}}},
    {"id": "split-by", "tag": "chart types", "query": "For melanoma trials, break down each phase by recruitment status.",
     "expect": {"viz_type": "grouped_bar_chart", "group_by": {"phase", "overall_status"}, "split_by": {"phase", "overall_status"}}},
    {"id": "network-condition-drug", "tag": "chart types", "query": "Show a network of conditions and drugs for trials sponsored by Pfizer since 2020.",
     "expect": {"viz_type": "network_graph", "network": ("condition", "drug"), "entity": {"sponsor": "pfizer"}, "start_year": 2020}},
    {"id": "network-investigator", "tag": "chart types", "query": "Which investigators work with which sponsors in ALS research?",
     "expect": {"viz_type": "network_graph", "network": ("investigator", "lead_sponsor")}},
    # --- answer modes ------------------------------------------------------------------------------
    {"id": "metric", "tag": "answer mode", "query": "How many phase 3 Alzheimer's trials have been terminated?",
     "expect": {"answer_mode": "single_value", "viz_type": "metric", "status": {"TERMINATED"}, "phase": {"PHASE3"}}},
    {"id": "unsupported-efficacy", "tag": "answer mode", "query": "Does metformin reduce cancer risk?",
     "expect": {"answer_mode": "unsupported"}},
    {"id": "unsupported-offtopic", "tag": "answer mode", "query": "What's the weather in Boston tomorrow?",
     "expect": {"answer_mode": "unsupported"}},
    {"id": "unsupported-safety", "tag": "answer mode", "query": "What are the side effects of semaglutide?",
     "expect": {"answer_mode": "unsupported"}},
    # --- tricky phrasing ---------------------------------------------------------------------------
    {"id": "brand-names", "tag": "tricky", "query": "Keytruda vs Opdivo, how do their trials break down by phase?",
     "expect": {"viz_type": "grouped_bar_chart", "group_by": "phase", "cohorts": 2}},
    {"id": "top-n-sponsors", "tag": "tricky", "query": "top 5 sponsors running phase 3 obesity studies",
     "expect": {"viz_type": "bar_chart", "group_by": "lead_sponsor", "top_n": 5, "phase": {"PHASE3"}, "entity": {"condition": "obes"}}},
    {"id": "implicit-recruiting", "tag": "tricky", "query": "Where in the world is semaglutide being tested right now?",
     "expect": {"group_by": {"recruiting_country", "country"}, "entity": {"intervention": "semaglutide"}}},
    {"id": "typo", "tag": "tricky", "query": "pembrolizmab trails per year",
     "expect": {"viz_type": "time_series", "group_by": "start_year", "entity": {"intervention": "pembrolizumab"}}},
    {"id": "chinese", "tag": "tricky", "query": "乳腺癌试验在各个阶段是怎么分布的？",
     "expect": {"viz_type": "bar_chart", "group_by": "phase", "entity": {"condition": "breast"}}},
    {"id": "vague-trend", "tag": "tricky", "query": "trends in hepatitis B research",
     "expect": {"viz_type": "time_series", "group_by": "start_year", "entity": {"condition": "hepatitis b"}}},
    # --- structured fields override ------------------------------------------------------------------
    {"id": "structured-compare", "tag": "structured", "query": "Compare these drugs by phase.",
     "fields": {"drug_name": ["semaglutide", "tirzepatide"]},
     "expect": {"viz_type": "grouped_bar_chart", "group_by": "phase", "cohorts": 2}},
]


def _filters(plan: QueryPlan, field: str) -> list[str]:
    values = [getattr(plan.shared_filters, field)] + [getattr(c.filters, field) for c in plan.cohorts]
    return [str(v).casefold() for v in values if v]


def check(plan: QueryPlan, expect: dict) -> list[str]:
    """Return the failed checks (empty = pass)."""
    failed = []

    def want(name: str, ok: bool) -> None:
        if not ok:
            failed.append(name)

    value = lambda e: e.value if e is not None else None  # noqa: E731
    if "answer_mode" in expect:
        want("answer_mode", plan.answer_mode.value == expect["answer_mode"])
    if "viz_type" in expect:
        want("viz_type", value(plan.viz_type) == expect["viz_type"])
    for key in ("group_by", "split_by"):
        if key in expect:
            accepted = expect[key] if isinstance(expect[key], set) else {expect[key]}
            want(key, value(getattr(plan, key)) in accepted)
    if "split_by" in expect and "group_by" in expect and isinstance(expect["group_by"], set):
        want("group_by != split_by", value(plan.group_by) != value(plan.split_by))
    if "network" in expect:
        got = sorted([value(plan.network.source_dim), value(plan.network.target_dim)]) if plan.network else None
        want("network", got == sorted(expect["network"]))
    if "measure" in expect:
        got = (value(plan.measure.x), value(plan.measure.y)) if plan.measure else None
        want("measure", got == expect["measure"])
    for field, substring in expect.get("entity", {}).items():
        want(f"{field}~{substring}", any(substring in v for v in _filters(plan, field)))
    for field in ("status", "phase"):
        if field in expect:
            got = {s.value for f in [plan.shared_filters, *(c.filters for c in plan.cohorts)] for s in (getattr(f, field) or [])}
            want(field, got == expect[field])
    if "start_year" in expect:
        want("start_year", expect["start_year"] in [plan.shared_filters.start_year, *(c.filters.start_year for c in plan.cohorts)])
    if "top_n" in expect:
        want("top_n", plan.top_n == expect["top_n"])
    if "cohorts" in expect:
        want("cohorts", len(plan.cohorts) == expect["cohorts"])
    return failed


CTGOV = CtgovClient()


def count_trials(params: dict[str, str]) -> int:
    return CTGOV.count(params)[0]


def zero_hit_terms(plan: QueryPlan) -> list[str]:
    """End-to-end check: every search term in the final plan must match at least one trial."""
    return [
        f"0 trials for {field} '{getattr(f, field)}'"
        for f in [plan.shared_filters, *(c.filters for c in plan.cohorts)]
        for field in ENTITY_FIELDS
        if getattr(f, field) and count_trials(build_params(_only(field, getattr(f, field)))) == 0
    ]


def run(model: str) -> list[dict]:
    planner = Planner(OpenAIPlanner(model=model), count_trials)
    results = []
    for case in CASES:
        request = VisualizeRequest(query=case["query"], **case.get("fields", {}))
        start = time.time()
        try:
            plan = planner.plan(request)
            failed = check(plan, case["expect"]) + zero_hit_terms(plan)
            notes = plan.notes
            plan_json = plan.model_dump(mode="json", exclude={"notes", "max_records"})
        except PlanRejected as exc:
            failed, notes, plan_json = [f"rejected: {exc}"], [], None
        results.append({
            "id": case["id"], "tag": case["tag"], "query": case["query"], "passed": not failed, "failed": failed,
            "retried": any("validation retry" in n for n in notes), "fallback": any("fallback" in n for n in notes),
            "grounded": any("verified against the API" in n for n in notes),
            "seconds": round(time.time() - start, 2), "plan": plan_json,
        })
        print(f"  {model:14s} {case['id']:24s} {'PASS' if not failed else 'FAIL ' + ', '.join(failed)}")
    return results


def write_report(all_results: dict[str, list[dict]]) -> None:
    models = list(all_results)
    lines = [
        "# Planner evaluation", "",
        f"{len(CASES)} labeled questions, each run through the full planner (LLM structured output, plan rules, "
        "one corrective retry, keyword fallback, search-term grounding against the live API). A case passes only "
        "if every expected field matches AND every search term in the final plan matches at least one trial. "
        "Generated by `scripts/eval_planner.py`; raw plans in `planner_eval.json`.", "",
        "| Model | Passed | Accuracy | Rule retry | Search-term correction | Keyword fallback | Median latency |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in models:
        rs = all_results[m]
        passed = sum(r["passed"] for r in rs)
        secs = sorted(r["seconds"] for r in rs)
        lines.append(
            f"| `{m}` | {passed}/{len(rs)} | {passed / len(rs):.0%} | {sum(r['retried'] for r in rs)} | "
            f"{sum(r['grounded'] for r in rs)} | "
            f"{sum(r['fallback'] for r in rs)} | {secs[len(secs) // 2]:.1f} s |"
        )
    lines += ["", "| Case | Tag | Question | " + " | ".join(f"`{m}`" for m in models) + " |",
              "|---|---|---|" + "---|" * len(models)]
    for i, case in enumerate(CASES):
        cells = []
        for m in models:
            r = all_results[m][i]
            cells.append("✅" if r["passed"] else "❌ " + ", ".join(r["failed"]))
        q = case["query"] + (f" + `{json.dumps(case['fields'])}`" if case.get("fields") else "")
        lines.append(f"| {case['id']} | {case['tag']} | {q} | " + " | ".join(cells) + " |")
    (DOCS / "planner_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (DOCS / "planner_eval.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    models = sys.argv[1:] or DEFAULT_MODELS
    results = {}
    for m in models:
        print(f"== {m}")
        results[m] = run(m)
    write_report(results)
    print(f"wrote {DOCS / 'planner_eval.md'}")
