"""PlanFilters -> ClinicalTrials.gov /studies query parameters.

Syntax verified against the live API (PLAN.md §9):
- drugs: AREA[InterventionName]"x" OR AREA[InterventionMeshTerm]"x". Plain query.intr applies
  concept expansion and over-matches ~12%; InterventionName already matches registered synonyms.
- multi-word values must be quoted, otherwise only the first word is bound to the AREA.
- phases / dates / study type go into filter.advanced, combined with AND.
"""

from __future__ import annotations

import re

from app.schemas.enums import OverallStatus, Phase
from app.schemas.plan import PlanFilters

_ESSIE_SPECIAL = re.compile(r'["\[\]()]')
_WS = re.compile(r"\s+")


def sanitize(term: str) -> str:
    """Strip characters that would break an Essie expression."""
    return _WS.sub(" ", _ESSIE_SPECIAL.sub(" ", term)).strip()


def drug_query(name: str) -> str:
    term = sanitize(name)
    return f'(AREA[InterventionName]"{term}" OR AREA[InterventionMeshTerm]"{term}")'


def phase_expr(phases: list[Phase]) -> str:
    return "(" + " OR ".join(f"AREA[Phase]{p.value}" for p in phases) + ")"


def start_year_expr(start_year: int | None, end_year: int | None) -> str:
    lo = f"{start_year}-01-01" if start_year else "MIN"
    hi = f"{end_year}-12-31" if end_year else "MAX"
    return f"AREA[StartDate]RANGE[{lo},{hi}]"


def merge_filters(shared: PlanFilters, cohort: PlanFilters | None) -> PlanFilters:
    """Cohort-specific values override shared ones."""
    if cohort is None:
        return shared
    merged = shared.model_dump()
    merged.update({k: v for k, v in cohort.model_dump().items() if v is not None})
    return PlanFilters(**merged)


def build_params(filters: PlanFilters, extra_advanced: list[str] | None = None) -> dict[str, str]:
    """Search / filter parameters only; paging and `fields` are added by the client."""
    params: dict[str, str] = {}
    if filters.intervention:
        params["query.intr"] = drug_query(filters.intervention)
    if filters.condition:
        params["query.cond"] = sanitize(filters.condition)
    if filters.sponsor:
        params["query.lead"] = sanitize(filters.sponsor)
    if filters.country:
        params["query.locn"] = sanitize(filters.country)
    if filters.status:
        params["filter.overallStatus"] = ",".join(OverallStatus(s).value for s in filters.status)

    advanced = list(extra_advanced or [])
    if filters.phase:
        advanced.append(phase_expr(filters.phase))
    if filters.study_type:
        advanced.append(f"AREA[StudyType]{filters.study_type.value}")
    if filters.start_year or filters.end_year:
        advanced.append(start_year_expr(filters.start_year, filters.end_year))
    if advanced:
        params["filter.advanced"] = " AND ".join(advanced)
    return params


def has_entity_filter(filters: PlanFilters) -> bool:
    """True if the search is scoped by a drug, condition, sponsor or country (PLAN.md §5 scope guard)."""
    return any([filters.intervention, filters.condition, filters.sponsor, filters.country])
