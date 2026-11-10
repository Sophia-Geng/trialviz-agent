"""Planner orchestration: LLM plan -> business rules -> one corrective retry -> keyword fallback.

    request ──► LLM ──► check() ──ok──► apply_request() ──► apply_scope_guard() ──► QueryPlan
                 ▲         │
                 └─retry 1─┘ (violations fed back)   still invalid / LLM down ──► fallback_plan()
"""

from __future__ import annotations

import logging

from app.planner.fallback import fallback_plan
from app.planner.llm_client import LLMClient, PlannerError
from app.planner.prompts import build_messages
from app.schemas.plan import QueryPlan, QueryPlanLLM
from app.schemas.request import VisualizeRequest
from app.validation import plan_rules
from app.validation.plan_rules import PlanRejected

log = logging.getLogger(__name__)


class Planner:
    def __init__(self, llm: LLMClient | None) -> None:
        self._llm = llm

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
            if not errors:
                return plan, []
            log.info("plan rejected, retrying: %s", errors)
            messages += [
                {"role": "assistant", "content": plan.model_dump_json()},
                {"role": "user", "content": "That plan violates these rules: " + "; ".join(errors)
                 + ". Return a corrected plan for the same question."},
            ]
            plan = self._llm.generate_plan(messages)
            errors = plan_rules.check(plan)
            if not errors:
                return plan, ["Planner output was corrected after one validation retry."]
            return None, [f"Planner output failed validation twice ({'; '.join(errors)}); used keyword fallback."]
        except PlannerError as exc:
            log.warning("LLM planner failed: %s", exc)
            return None, [f"LLM planner unavailable ({exc}); used keyword fallback."]


def _structured_context(request: VisualizeRequest) -> str | None:
    fields = request.model_dump(exclude={"query", "max_records", "top_n"}, exclude_none=True, mode="json")
    return ", ".join(f"{k}={v}" for k, v in fields.items()) or None
