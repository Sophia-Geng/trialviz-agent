"""Normalization of raw ClinicalTrials.gov values (PLAN.md §10).

Pure functions only; the raw value is always kept separately for citations.
"""

from __future__ import annotations

import re
from datetime import date

UNKNOWN = "Unknown"
NOT_APPLICABLE = "Not Applicable"

# --- text --------------------------------------------------------------------

_WS = re.compile(r"\s+")


def text_key(s: str) -> str:
    """Case- and whitespace-insensitive grouping key."""
    return _WS.sub(" ", s).strip().casefold()


def clean_label(s: str) -> str:
    return _WS.sub(" ", s).strip()


def humanize_enum(value: str) -> str:
    """'ACTIVE_NOT_RECRUITING' -> 'Active not recruiting'."""
    return value.replace("_", " ").capitalize()


SPONSOR_CLASS_LABELS = {
    "NIH": "NIH",
    "FED": "U.S. Federal",
    "OTHER_GOV": "Other government",
    "INDIV": "Individual",
    "INDUSTRY": "Industry",
    "NETWORK": "Network",
    "AMBIG": "Ambiguous",
    "OTHER": "Other",
    "UNKNOWN": UNKNOWN,
}

# --- phase -------------------------------------------------------------------

_PHASE_NUMBER = {"EARLY_PHASE1": "1", "PHASE1": "1", "PHASE2": "2", "PHASE3": "3", "PHASE4": "4"}

PHASE_ORDER = [
    "Early Phase 1",
    "Phase 1",
    "Phase 1/2",
    "Phase 2",
    "Phase 2/3",
    "Phase 3",
    "Phase 4",
    NOT_APPLICABLE,
    UNKNOWN,
]


def normalize_phase(phases: list[str] | None) -> str:
    """Collapse the API's phase list into one label, so phase buckets sum to the trial count.

    ["PHASE1", "PHASE2"] -> "Phase 1/2"; ["NA"] -> "Not Applicable"; [] / None -> "Unknown".
    """
    if not phases:
        return UNKNOWN
    values = set(phases)
    if values == {"EARLY_PHASE1"}:
        return "Early Phase 1"
    numbers = sorted({_PHASE_NUMBER[p] for p in values if p in _PHASE_NUMBER})
    if numbers:
        return "Phase " + "/".join(numbers)
    if "NA" in values:
        return NOT_APPLICABLE
    return UNKNOWN


def phase_sort_key(label: str) -> int:
    try:
        return PHASE_ORDER.index(label)
    except ValueError:  # unusual combinations such as "Phase 1/2/3"
        return PHASE_ORDER.index(NOT_APPLICABLE) - 1


# --- dates -------------------------------------------------------------------

_DATE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")


def parse_date(raw: str | None) -> date | None:
    """Parse 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Missing month/day default to 1."""
    if not raw:
        return None
    m = _DATE.match(raw.strip())
    if not m:
        return None
    year, month, day = int(m[1]), int(m[2] or 1), int(m[3] or 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def months_between(start: date, end: date) -> float:
    return round((end - start).days / 30.4375, 1)


# --- interventions -------------------------------------------------------------

_PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_TRADEMARK = re.compile(r"[®™©]")
_DOSE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:mg/m2|mg/m²|mg/kg|mg|mcg|µg|ug|g|ml|iu|units?|%)(?=\W|$).*$",
    re.IGNORECASE,
)
_NON_DRUG = re.compile(
    r"\b(placebo|saline|sham|vehicle|standard of care|best supportive care|usual care|no intervention)\b",
    re.IGNORECASE,
)

# MeSH intervention terms that are procedures or broad classes rather than drugs.
MESH_NON_DRUG = {
    "radiotherapy",
    "radiation",
    "drug therapy",
    "specimen handling",
    "watchful waiting",
    "observation",
    "magnetic resonance spectroscopy",
    "taxes",
    "pharmaceutical solutions",
    "antibodies",
    "antibodies, monoclonal",
    "vaccines",
}


def clean_drug_name(raw: str) -> str:
    """'Pembrolizumab 200 mg IV (MK-3475)' -> 'pembrolizumab'. Returns '' if nothing is left."""
    s = _TRADEMARK.sub("", raw)
    s = _PARENS.sub(" ", s)
    s = _DOSE.sub("", s)
    s = text_key(s).strip(" -,;:.")
    return s


_COMBINATION_SEP = re.compile(r"\s*(?:\+|/|\bplus\b|\bcombined with\b|\bwith\b|\band\b)\s*", re.IGNORECASE)


def split_combination(name: str) -> list[str]:
    """'Gemcitabine+ nivolumab' -> ['Gemcitabine', 'nivolumab']. Hyphens are kept ('5-fluorouracil')."""
    return [part for part in _COMBINATION_SEP.split(name) if part.strip()]


def is_non_drug(name: str) -> bool:
    return bool(_NON_DRUG.search(name))


# --- sites & investigators ---------------------------------------------------

_PLACEHOLDER_SITE = re.compile(
    r"^(site|research site|investigational site|investigative site|clinical site|study site|local institution)\b"
    r"|\b(research|investigational|investigative|clinical|study) site\b"
    r"|additional information",
    re.IGNORECASE,
)

_PLACEHOLDER_OFFICIAL = re.compile(
    r"medical director|medical monitor|call center|clinical trials?\b|study director|study physician"
    r"|clinical development|clinical operations|ct\.gov|central contact|trial manager|sponsor",
    re.IGNORECASE,
)

INVESTIGATOR_ROLES = {"PRINCIPAL_INVESTIGATOR", "STUDY_CHAIR"}


def is_placeholder_site(facility: str) -> bool:
    return bool(_PLACEHOLDER_SITE.search(facility))


def is_placeholder_official(name: str) -> bool:
    return bool(_PLACEHOLDER_OFFICIAL.search(name))
