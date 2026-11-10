"""Planner orchestration: LLM plan -> business rules -> one corrective retry -> keyword fallback,
then grounding of the plan's search terms against the live registry.

    request ──► LLM ──► check() ──ok──► ground entities ──► apply_request() ──► apply_scope_guard() ──► QueryPlan
                 ▲         │                 │
                 └─retry 1─┘                 └─ a term matches 0 trials (typo, non-English) ──► LLM fixes only
                 (violations fed back)          those terms, kept only if the API confirms fewer zero-hit terms
    still invalid / LLM down ──► fallback_plan()

The prompt tells the model to copy names as written, never to "translate" them from memory. Grounding
is the controlled exception: a correction is requested only when the API proves a term finds nothing,
and accepted only when the API confirms the corrected term does better.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.ctgov.query_builder import build_params
from app.planner.fallback import fallback_plan
from app.planner.llm_client import LLMClient, PlannerError
from app.planner.prompts import build_messages
from app.schemas.enums import AnswerMode
from app.schemas.plan import PlanFilters, QueryPlan, QueryPlanLLM
from app.schemas.request import VisualizeRequest
from app.validation import plan_rules
from app.validation.plan_rules import PlanRejected

log = logging.getLogger(__name__)

ENTITY_FIELDS = ("intervention", "condition", "sponsor", "country")
# Counts the trials matching a set of /studies search parameters (CtgovClient.count in production).
TrialCounter = Callable[[dict[str, str]], int]


class Planner:
    def __init__(self, llm: LLMClient | None, count_trials: TrialCounter | None = None) -> None:
        self._llm = llm
        self._count = count_trials

    def plan(self, request: VisualizeRequest) -> QueryPlan:
        llm_plan, notes = self._llm_plan(request)
        if llm_plan is None:
            llm_plan = fallback_plan(request.query)
        plan = plan_rules.to_internal(llm_plan, request)
        plan = plan.model_copy(update={"notes": [*notes, *plan.notes]})
        plan = plan_rules.apply_request(plan, request)
        plan = plan_rules.apply_scope_guard(plan)

        # Explicit request fields (e.g. viz_type) can still produce an impossible plan.
        errors = plan_rules.check(QueryPlanLLM(**plan.model_dump(exclude={"max_records", "notes"})))
        if errors:
            raise PlanRejected(errors)
        return plan

    def _llm_plan(self, request: VisualizeRequest) -> tuple[QueryPlanLLM | None, list[str]]:
        if self._llm is None:
            return None, ["LLM planner not configured (OPENAI_API_KEY missing); used keyword fallback."]
        messages = build_messages(request.query, _structured_context(request))
        try:
            plan = self._llm.generate_plan(messages)
            errors = plan_rules.check(plan)
            notes: list[str] = []
            if errors:
                log.info("plan rejected, retrying: %s", errors)
                messages += [
                    {"role": "assistant", "content": plan.model_dump_json()},
                    {
                        "role": "user",
                        "content": "That plan violates these rules: "
                        + "; ".join(errors)
                        + ". Return a corrected plan for the same question.",
                    },
                ]
                plan = self._llm.generate_plan(messages)
                errors = plan_rules.check(plan)
                if errors:
                    return None, [
                        f"Planner output failed validation twice ({'; '.join(errors)}); used keyword fallback."
                    ]
                notes.append("Planner output was corrected after one validation retry.")
            plan, grounding_notes = self._ground_entities(plan, messages)
            return plan, notes + grounding_notes
        except PlannerError as exc:
            log.warning("LLM planner failed: %s", exc)
            return None, [f"LLM planner unavailable ({exc}); used keyword fallback."]

    # --- grounding -------------------------------------------------------------

    def _zero_hit_terms(self, plan: QueryPlanLLM) -> list[tuple[str, str]]:
        """(field, value) search terms that match no trial on their own."""
        found = []
        for filters in [plan.shared_filters, *(c.filters for c in plan.cohorts)]:
            for field in ENTITY_FIELDS:
                value = getattr(filters, field)
                if value and (field, value) not in found and self._count(build_params(_only(field, value))) == 0:
                    found.append((field, value))
        return found

    def _ground_entities(self, plan: QueryPlanLLM, messages: list[dict]) -> tuple[QueryPlanLLM, list[str]]:
        if self._count is None or plan.answer_mode is AnswerMode.UNSUPPORTED:
            return plan, []
        missing = self._zero_hit_terms(plan)
        if not missing:
            return plan, []
        terms = ", ".join(f"{field} '{value}'" for field, value in missing)
        log.info("zero-hit search terms, asking for correction: %s", terms)
        fixed = self._llm.generate_plan(
            [
                *messages,
                {"role": "assistant", "content": plan.model_dump_json()},
                {
                    "role": "user",
                    "content": f"On ClinicalTrials.gov these search terms match no trials: {terms}. "
                    "They may be misspelled or not in English. Return the same plan with only these terms "
                    "replaced by the standard English name used in trial registries; change nothing else.",
                },
            ]
        )
        still_missing = self._zero_hit_terms(fixed) if not plan_rules.check(fixed) else missing
        if len(still_missing) >= len(missing):
            return plan, [f"Search terms that match no trials on ClinicalTrials.gov: {terms}. Check the spelling."]
        corrected = [
            f"{field} '{value}' -> '{new}'"
            for (field, value), new in zip(missing, (_find(fixed, f) for f, _ in missing), strict=True)
            if (field, value) not in still_missing
        ]
        return fixed, [
            f"Search terms matched no trials and were corrected by the planner, verified against the "
            f"API: {', '.join(corrected)}."
        ]


def _only(field: str, value: str) -> PlanFilters:
    return PlanFilters(**{**{name: None for name in PlanFilters.model_fields}, field: value})


def _find(plan: QueryPlanLLM, field: str) -> str:
    values = [getattr(f, field) for f in [plan.shared_filters, *(c.filters for c in plan.cohorts)] if getattr(f, field)]
    return ", ".join(dict.fromkeys(values))


def _structured_context(request: VisualizeRequest) -> str | None:
    fields = request.model_dump(exclude={"query", "max_records", "top_n"}, exclude_none=True, mode="json")
    return ", ".join(f"{k}={v}" for k, v in fields.items()) or None
