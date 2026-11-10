"""Dimension registry: the single extension point of the system (PLAN.md §6).

A dimension knows which API fields it needs and how to turn one study into
zero or more `Extracted` values. Each value carries both the normalized key
used for grouping and the exact raw excerpt + field path used for citations.
Adding a question type usually means adding one entry here.

Missing values: categorical dimensions yield "Unknown"; temporal and numeric
dimensions yield nothing (the aggregator reports how many were skipped).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.data import normalize as n
from app.schemas.enums import DimensionKind, DimensionName

P = "protocolSection"
STATUS = f"{P}.statusModule"
DESIGN = f"{P}.designModule"
SPONSORS = f"{P}.sponsorCollaboratorsModule"
INTERVENTIONS = f"{P}.armsInterventionsModule.interventions"
LOCATIONS = f"{P}.contactsLocationsModule.locations"
OFFICIALS = f"{P}.contactsLocationsModule.overallOfficials"
INTERVENTION_MESH = "derivedSection.interventionBrowseModule.meshes"

MISSING_EXCERPT = "(field not present in API response)"
BASE_FIELDS = ("NCTId", "BriefTitle")
DRUG_INTERVENTION_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT"}


@dataclass(frozen=True)
class Extracted:
    key: str | float  # normalized grouping key (numeric for temporal / numeric dimensions)
    label: str  # display label
    excerpt: str  # exact raw value from the API response
    field_path: str  # concrete path, including array index
    estimated: bool = False  # date-derived value based on an ESTIMATED date


Extractor = Callable[[dict[str, Any]], list[Extracted]]


@dataclass(frozen=True)
class Dimension:
    name: DimensionName
    kind: DimensionKind
    label: str
    description: str  # shown to the planner
    api_fields: tuple[str, ...]
    multi_valued: bool
    extract: Extractor


# --- helpers -----------------------------------------------------------------


def get(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def nct_id(study: dict) -> str:
    return get(study, f"{P}.identificationModule.nctId")


def brief_title(study: dict) -> str | None:
    return get(study, f"{P}.identificationModule.briefTitle")


def _unknown(path: str) -> list[Extracted]:
    return [Extracted(n.UNKNOWN, n.UNKNOWN, MISSING_EXCERPT, path)]


def _dedupe(values: list[Extracted]) -> list[Extracted]:
    """A trial counts once per distinct key."""
    seen: set = set()
    out = []
    for v in values:
        if v.key not in seen:
            seen.add(v.key)
            out.append(v)
    return out


def _enum_field(path: str, labels: dict[str, str] | None = None) -> Extractor:
    def extract(study: dict) -> list[Extracted]:
        raw = get(study, path)
        if not raw:
            return _unknown(path)
        label = (labels or {}).get(raw) or n.humanize_enum(raw)
        return [Extracted(raw, label, raw, path)]

    return extract


def _text_field(path: str) -> Extractor:
    def extract(study: dict) -> list[Extracted]:
        raw = get(study, path)
        if not raw or not raw.strip():
            return _unknown(path)
        return [Extracted(n.text_key(raw), n.clean_label(raw), raw, path)]

    return extract


# --- extractors --------------------------------------------------------------


def _phase(study: dict) -> list[Extracted]:
    path = f"{DESIGN}.phases"
    raw = get(study, path)
    label = n.normalize_phase(raw)
    excerpt = json.dumps(raw) if raw else MISSING_EXCERPT
    return [Extracted(label, label, excerpt, path)]


def _start_year(study: dict) -> list[Extracted]:
    path = f"{STATUS}.startDateStruct.date"
    raw = get(study, path)
    parsed = n.parse_date(raw)
    if parsed is None:
        return []
    estimated = get(study, f"{STATUS}.startDateStruct.type") == "ESTIMATED"
    return [Extracted(float(parsed.year), str(parsed.year), raw, path, estimated)]


def _intervention_type(study: dict) -> list[Extracted]:
    interventions = get(study, INTERVENTIONS) or []
    out = []
    for i, iv in enumerate(interventions):
        raw = iv.get("type")
        if raw:
            out.append(Extracted(raw, n.humanize_enum(raw), raw, f"{INTERVENTIONS}[{i}].type"))
    return _dedupe(out) or _unknown(INTERVENTIONS)


def _intervention_name(study: dict) -> list[Extracted]:
    interventions = get(study, INTERVENTIONS) or []
    out = []
    for i, iv in enumerate(interventions):
        raw = iv.get("name")
        if not raw or n.is_non_drug(raw):
            continue
        cleaned = n.clean_drug_name(raw)
        if cleaned:
            out.append(Extracted(cleaned, cleaned, raw, f"{INTERVENTIONS}[{i}].name"))
    return _dedupe(out) or _unknown(INTERVENTIONS)


def _drug(study: dict) -> list[Extracted]:
    """Drugs via MeSH terms (merges synonyms like MK-3475 / Keytruda), falling back to cleaned names."""
    interventions = get(study, INTERVENTIONS) or []
    drug_ivs = [(i, iv) for i, iv in enumerate(interventions) if iv.get("type") in DRUG_INTERVENTION_TYPES]
    if not drug_ivs:
        return []

    out = []
    for i, mesh in enumerate(get(study, INTERVENTION_MESH) or []):
        term = mesh.get("term")
        if term and n.text_key(term) not in n.MESH_NON_DRUG:
            key = n.text_key(term)
            out.append(Extracted(key, key, term, f"{INTERVENTION_MESH}[{i}].term"))
    if out:
        return _dedupe(out)

    # No usable MeSH terms: fall back to registered names, splitting combinations like 'A+B'.
    for i, iv in drug_ivs:
        raw = iv.get("name")
        if not raw:
            continue
        for part in n.split_combination(raw):
            cleaned = n.clean_drug_name(part)
            if cleaned and not n.is_non_drug(cleaned):
                out.append(Extracted(cleaned, cleaned, raw, f"{INTERVENTIONS}[{i}].name"))
    return _dedupe(out)


def _condition(study: dict) -> list[Extracted]:
    path = f"{P}.conditionsModule.conditions"
    out = [
        Extracted(n.text_key(raw), n.clean_label(raw), raw, f"{path}[{i}]")
        for i, raw in enumerate(get(study, path) or [])
        if raw and raw.strip()
    ]
    return _dedupe(out) or _unknown(path)


def _countries(study: dict, recruiting_only: bool) -> list[Extracted]:
    locations = get(study, LOCATIONS) or []
    if recruiting_only and not any("status" in loc for loc in locations):
        # No site-level status: fall back to the trial-level status.
        if get(study, f"{STATUS}.overallStatus") != "RECRUITING":
            return []
        recruiting_only = False
    out = []
    for i, loc in enumerate(locations):
        raw = loc.get("country")
        if not raw or (recruiting_only and loc.get("status") != "RECRUITING"):
            continue
        out.append(Extracted(n.text_key(raw), n.clean_label(raw), raw, f"{LOCATIONS}[{i}].country"))
    if recruiting_only:
        return _dedupe(out)
    return _dedupe(out) or _unknown(LOCATIONS)


def _site(study: dict) -> list[Extracted]:
    locations = get(study, LOCATIONS) or []
    if not locations:
        return _unknown(LOCATIONS)
    out = []
    for i, loc in enumerate(locations):
        raw = loc.get("facility")
        if raw and raw.strip() and not n.is_placeholder_site(raw):
            out.append(Extracted(n.text_key(raw), n.clean_label(raw), raw, f"{LOCATIONS}[{i}].facility"))
    return _dedupe(out)


def _investigator(study: dict) -> list[Extracted]:
    out = []
    for i, off in enumerate(get(study, OFFICIALS) or []):
        raw = off.get("name")
        if not raw or off.get("role") not in n.INVESTIGATOR_ROLES or n.is_placeholder_official(raw):
            continue
        affiliation = off.get("affiliation")
        label = f"{n.clean_label(raw)} ({affiliation})" if affiliation else n.clean_label(raw)
        out.append(Extracted(n.text_key(raw), label, raw, f"{OFFICIALS}[{i}].name"))
    return _dedupe(out)


def _enrollment(study: dict) -> list[Extracted]:
    path = f"{DESIGN}.enrollmentInfo.count"
    raw = get(study, path)
    if not isinstance(raw, int | float):
        return []
    return [Extracted(float(raw), str(raw), str(raw), path)]


def _duration_months(study: dict) -> list[Extracted]:
    start_raw = get(study, f"{STATUS}.startDateStruct.date")
    end_raw = get(study, f"{STATUS}.completionDateStruct.date")
    start, end = n.parse_date(start_raw), n.parse_date(end_raw)
    if start is None or end is None or end < start:
        return []
    months = n.months_between(start, end)
    estimated = "ESTIMATED" in (
        get(study, f"{STATUS}.startDateStruct.type"),
        get(study, f"{STATUS}.completionDateStruct.type"),
    )
    excerpt = f"startDateStruct.date={start_raw}; completionDateStruct.date={end_raw}"
    path = f"{STATUS}.{{startDateStruct,completionDateStruct}}.date"
    return [Extracted(months, f"{months:g}", excerpt, path, estimated)]


def _site_count(study: dict) -> list[Extracted]:
    locations = get(study, LOCATIONS) or []
    if not locations:
        return []
    count = len(locations)
    return [Extracted(float(count), str(count), f"{count} entries in locations[]", LOCATIONS)]


# --- registry ----------------------------------------------------------------

D, K = DimensionName, DimensionKind

DIMENSIONS: dict[DimensionName, Dimension] = {
    d.name: d
    for d in [
        Dimension(D.PHASE, K.CATEGORICAL, "Phase", "Trial phase; combined phases appear as e.g. 'Phase 1/2'.",
                  ("Phase",), False, _phase),
        Dimension(D.START_YEAR, K.TEMPORAL, "Start year", "Year the trial started (actual or estimated).",
                  ("StartDate", "StartDateType"), False, _start_year),
        Dimension(D.OVERALL_STATUS, K.CATEGORICAL, "Status", "Overall recruitment status.",
                  ("OverallStatus",), False, _enum_field(f"{STATUS}.overallStatus")),
        Dimension(D.STUDY_TYPE, K.CATEGORICAL, "Study type", "Interventional, observational or expanded access.",
                  ("StudyType",), False, _enum_field(f"{DESIGN}.studyType")),
        Dimension(D.LEAD_SPONSOR, K.CATEGORICAL, "Lead sponsor", "Lead sponsor organization name.",
                  ("LeadSponsorName",), False, _text_field(f"{SPONSORS}.leadSponsor.name")),
        Dimension(D.SPONSOR_CLASS, K.CATEGORICAL, "Sponsor category", "Lead sponsor category: industry, NIH, other...",
                  ("LeadSponsorClass",), False,
                  _enum_field(f"{SPONSORS}.leadSponsor.class", n.SPONSOR_CLASS_LABELS)),
        Dimension(D.INTERVENTION_TYPE, K.CATEGORICAL, "Intervention type", "Drug, device, behavioral, procedure...",
                  ("InterventionType",), True, _intervention_type),
        Dimension(D.INTERVENTION_NAME, K.CATEGORICAL, "Intervention",
                  "Any intervention by its registered name (drugs, devices, procedures...).",
                  ("InterventionName",), True, _intervention_name),
        Dimension(D.DRUG, K.CATEGORICAL, "Drug",
                  "Drugs/biologics, synonyms merged via MeSH. Prefer this for drug networks and drug rankings.",
                  ("InterventionType", "InterventionName", "InterventionMeshTerm"), True, _drug),
        Dimension(D.CONDITION, K.CATEGORICAL, "Condition", "Conditions / diseases studied.",
                  ("Condition",), True, _condition),
        Dimension(D.COUNTRY, K.CATEGORICAL, "Country", "Countries with at least one trial site.",
                  ("LocationCountry",), True, lambda s: _countries(s, recruiting_only=False)),
        Dimension(D.RECRUITING_COUNTRY, K.CATEGORICAL, "Country (recruiting sites)",
                  "Countries with at least one currently recruiting site. Use for 'where are trials recruiting'.",
                  ("LocationCountry", "LocationStatus", "OverallStatus"), True,
                  lambda s: _countries(s, recruiting_only=True)),
        Dimension(D.SITE, K.CATEGORICAL, "Site", "Trial site facility names (anonymous placeholders removed).",
                  ("LocationFacility",), True, _site),
        Dimension(D.INVESTIGATOR, K.CATEGORICAL, "Investigator",
                  "Principal investigators / study chairs (sponsor placeholders removed).",
                  ("OverallOfficialName", "OverallOfficialRole", "OverallOfficialAffiliation"), True, _investigator),
        Dimension(D.ENROLLMENT, K.NUMERIC, "Enrollment (participants)", "Number of participants enrolled.",
                  ("EnrollmentCount",), False, _enrollment),
        Dimension(D.DURATION_MONTHS, K.NUMERIC, "Duration (months)", "Months from start date to completion date.",
                  ("StartDate", "StartDateType", "CompletionDate", "CompletionDateType"), False, _duration_months),
        Dimension(D.SITE_COUNT, K.NUMERIC, "Number of sites", "Number of listed trial sites.",
                  ("LocationFacility",), False, _site_count),
    ]
}


def api_fields(dims: list[DimensionName]) -> str:
    """`fields=` parameter value covering the given dimensions."""
    fields = list(BASE_FIELDS)
    for d in dims:
        fields.extend(f for f in DIMENSIONS[d].api_fields if f not in fields)
    return "|".join(fields)
