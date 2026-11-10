"""Dimension registry: the single extension point of the system (README §6.2).

A dimension knows which API fields it needs and how to turn one study into
zero or more `Extracted` values. Each value carries both the normalized key
used for grouping and the exact raw excerpt + field path used for citations.
Adding a question type usually means adding one entry here.

Missing values: categorical dimensions yield "Unknown"; temporal and numeric
dimensions yield nothing (the aggregator reports how many were skipped).
"""

from __future__ import annotations

import json
import re
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
CONDITION_MESH = "derivedSection.conditionBrowseModule.meshes"

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
    # Weak evidence: kept only if it matches a non-tentative key of the same dimension
    # elsewhere in the batch (resolved by aggregate.engine.extract_batch).
    tentative: bool = False


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


def _text_field(path: str, key: Callable[[str], str] = n.text_key) -> Extractor:
    def extract(study: dict) -> list[Extracted]:
        raw = get(study, path)
        if not raw or not raw.strip():
            return _unknown(path)
        return [Extracted(key(raw), n.clean_label(raw), raw, path)]

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


def _name_parts(ivs: list[tuple[int, dict]], from_drug_type: bool) -> list[Extracted]:
    """Registered intervention names, split on combinations like 'A+B' and cleaned.

    A name is confirmed only if it comes from a drug-type intervention AND looks like a drug
    (INN stem such as -mab / -nib / -platin, or a development code such as HMPL-013). Everything
    else is tentative: kept only if it contains a drug confirmed elsewhere in the batch. This keeps
    mis-registered entries ('App', 'Test', 'Exercise' typed as DRUG) out of the drug dimension.
    """
    out = []
    for i, iv in ivs:
        raw = iv.get("name")
        if not raw:
            continue
        for part in n.split_combination(raw):
            if n.is_non_drug(part):  # before key normalization: 'sodium chloride' must not become 'sodium'
                continue
            cleaned = n.drug_key(n.clean_drug_name(part))
            if not cleaned or n.is_generic_fragment(cleaned):
                continue
            confirmed = from_drug_type and n.looks_like_drug(cleaned) and not n.is_mesh_non_drug(cleaned)
            out.append(Extracted(cleaned, cleaned, raw, f"{INTERVENTIONS}[{i}].name", tentative=not confirmed))
    return out


def _mentions(names: list[str], key: str) -> bool:
    """True if a MeSH key and a registered name (or other name) refer to each other as whole words."""
    pattern = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)")
    return any(pattern.search(name) or re.search(rf"(?<!\w){re.escape(name)}(?!\w)", key) for name in names if name)


def _drug(study: dict) -> list[Extracted]:
    """Drugs via MeSH terms (merges synonyms like MK-3475 / Keytruda), falling back to cleaned names.

    MeSH terms are attached to the whole trial, not to one intervention, so a behavioral arm adds
    terms like 'Mindfulness' next to the drugs. A MeSH term counts as a drug only with evidence:
    a supplementary chemical concept (id 'C...'), a trial whose interventions are all drugs, or a
    drug-type intervention whose name / other names mention the term.

    Drug regimens registered under a non-drug type (e.g. DEVICE 'Cyberknife plus Pembrolizumab')
    yield tentative values, kept only if they name a drug confirmed elsewhere in the batch.
    """
    interventions = get(study, INTERVENTIONS) or []
    drug_ivs = [(i, iv) for i, iv in enumerate(interventions) if iv.get("type") in DRUG_INTERVENTION_TYPES]
    other_ivs = [(i, iv) for i, iv in enumerate(interventions) if iv.get("type") not in DRUG_INTERVENTION_TYPES]
    # Placebo / control arms registered as OTHER do not make a trial "mixed".
    only_drugs = all(n.is_non_drug(iv.get("name") or "") for _, iv in other_ivs)
    drug_names = [
        n.text_key(name) for _, iv in drug_ivs for name in [iv.get("name") or "", *(iv.get("otherNames") or [])]
    ]

    confirmed = []
    if drug_ivs:
        for i, mesh in enumerate(get(study, INTERVENTION_MESH) or []):
            term = mesh.get("term")
            if not term or n.is_mesh_non_drug(term):
                continue
            key = n.drug_key(term)
            if only_drugs or str(mesh.get("id", "")).startswith("C") or _mentions(drug_names, key):
                confirmed.append(Extracted(key, key, term, f"{INTERVENTION_MESH}[{i}].term"))
        if not confirmed:
            confirmed = _name_parts(drug_ivs, from_drug_type=True)
    return _dedupe(confirmed + _name_parts(other_ivs, from_drug_type=False))


def _condition(study: dict) -> list[Extracted]:
    """Conditions via the API's condition MeSH terms ('Type 2 Diabetes' and 'Diabetes Mellitus, Type 2'
    are one value), falling back to the free-text conditions when a trial has no MeSH mapping
    (mostly healthy-volunteer studies, whose many spellings are merged). Known wrong MeSH mappings
    (normalize.KNOWN_CONDITION_MISMAPS) are dropped in favor of the free text."""
    path = f"{P}.conditionsModule.conditions"
    raw_conditions = [c for c in get(study, path) or [] if c and c.strip()]
    meshes = [(i, m.get("term")) for i, m in enumerate(get(study, CONDITION_MESH) or []) if m.get("term")]
    dropped = [term for _, term in meshes if n.is_condition_mismap(term, raw_conditions)]
    out = [
        Extracted(n.text_key(term), term, term, f"{CONDITION_MESH}[{i}].term")
        for i, term in meshes
        if term not in dropped
    ]
    if out and not dropped:
        return _dedupe(out)
    for i, raw in enumerate(get(study, path) or []):
        if not raw or not raw.strip():
            continue
        # after a dropped mis-mapping, restore only the free text it came from ('Analgesia')
        if out and not any(n.restores_mismap(term, raw) for term in dropped):
            continue
        key, label = (n.HEALTHY_KEY, n.HEALTHY_LABEL) if n.is_healthy(raw) else (n.text_key(raw), n.clean_label(raw))
        out.append(Extracted(key, label, raw, f"{path}[{i}]"))
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
        # 'Sean van Diepen, MD' and 'Sean van Diepen, MD, MSc' are one person: group on the name only.
        name = n.person_name(raw)
        affiliation = off.get("affiliation")
        label = f"{name} ({affiliation})" if affiliation else name
        out.append(Extracted(n.text_key(name), label, raw, f"{OFFICIALS}[{i}].name"))
    return _dedupe(out)


def _enrollment(study: dict) -> list[Extracted]:
    path = f"{DESIGN}.enrollmentInfo.count"
    raw = get(study, path)
    if not isinstance(raw, int | float):
        return []
    # Ongoing trials report a planned (ESTIMATED) enrollment, completed ones the ACTUAL number.
    estimated = get(study, f"{DESIGN}.enrollmentInfo.type") == "ESTIMATED"
    return [Extracted(float(raw), str(raw), str(raw), path, estimated)]


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
        Dimension(
            D.PHASE,
            K.CATEGORICAL,
            "Phase",
            "Trial phase; combined phases appear as e.g. 'Phase 1/2'.",
            ("Phase",),
            False,
            _phase,
        ),
        Dimension(
            D.START_YEAR,
            K.TEMPORAL,
            "Start year",
            "Year the trial started (actual or estimated).",
            ("StartDate", "StartDateType"),
            False,
            _start_year,
        ),
        Dimension(
            D.OVERALL_STATUS,
            K.CATEGORICAL,
            "Status",
            "Overall recruitment status.",
            ("OverallStatus",),
            False,
            _enum_field(f"{STATUS}.overallStatus"),
        ),
        Dimension(
            D.STUDY_TYPE,
            K.CATEGORICAL,
            "Study type",
            "Interventional, observational or expanded access.",
            ("StudyType",),
            False,
            _enum_field(f"{DESIGN}.studyType"),
        ),
        Dimension(
            D.LEAD_SPONSOR,
            K.CATEGORICAL,
            "Lead sponsor",
            "Lead sponsor organization name.",
            ("LeadSponsorName",),
            False,
            _text_field(f"{SPONSORS}.leadSponsor.name", n.organization_key),
        ),
        Dimension(
            D.SPONSOR_CLASS,
            K.CATEGORICAL,
            "Sponsor category",
            "Lead sponsor category: industry, NIH, other...",
            ("LeadSponsorClass",),
            False,
            _enum_field(f"{SPONSORS}.leadSponsor.class", n.SPONSOR_CLASS_LABELS),
        ),
        Dimension(
            D.INTERVENTION_TYPE,
            K.CATEGORICAL,
            "Intervention type",
            "Drug, device, behavioral, procedure...",
            ("InterventionType",),
            True,
            _intervention_type,
        ),
        Dimension(
            D.INTERVENTION_NAME,
            K.CATEGORICAL,
            "Intervention",
            "Any intervention by its registered name (drugs, devices, procedures...).",
            ("InterventionName",),
            True,
            _intervention_name,
        ),
        Dimension(
            D.DRUG,
            K.CATEGORICAL,
            "Drug",
            "Drugs/biologics, synonyms merged via MeSH. Prefer this for drug networks and drug rankings.",
            (
                "InterventionType",
                "InterventionName",
                "InterventionOtherName",
                "InterventionMeshTerm",
                "InterventionMeshId",
            ),
            True,
            _drug,
        ),
        Dimension(
            D.CONDITION,
            K.CATEGORICAL,
            "Condition",
            "Conditions / diseases studied.",
            ("Condition", "ConditionMeshTerm"),
            True,
            _condition,
        ),
        Dimension(
            D.COUNTRY,
            K.CATEGORICAL,
            "Country",
            "Countries with at least one trial site.",
            ("LocationCountry",),
            True,
            lambda s: _countries(s, recruiting_only=False),
        ),
        Dimension(
            D.RECRUITING_COUNTRY,
            K.CATEGORICAL,
            "Country (recruiting sites)",
            "Countries with at least one currently recruiting site. Use for 'where are trials recruiting'.",
            ("LocationCountry", "LocationStatus", "OverallStatus"),
            True,
            lambda s: _countries(s, recruiting_only=True),
        ),
        Dimension(
            D.SITE,
            K.CATEGORICAL,
            "Site",
            "Trial site facility names (anonymous placeholders removed).",
            ("LocationFacility",),
            True,
            _site,
        ),
        Dimension(
            D.INVESTIGATOR,
            K.CATEGORICAL,
            "Investigator",
            "Principal investigators / study chairs (sponsor placeholders removed).",
            ("OverallOfficialName", "OverallOfficialRole", "OverallOfficialAffiliation"),
            True,
            _investigator,
        ),
        Dimension(
            D.ENROLLMENT,
            K.NUMERIC,
            "Enrollment (participants)",
            "Number of participants (actual for completed trials, planned for ongoing ones).",
            ("EnrollmentCount", "EnrollmentType"),
            False,
            _enrollment,
        ),
        Dimension(
            D.DURATION_MONTHS,
            K.NUMERIC,
            "Duration (months)",
            "Months from start date to completion date.",
            ("StartDate", "StartDateType", "CompletionDate", "CompletionDateType"),
            False,
            _duration_months,
        ),
        Dimension(
            D.SITE_COUNT,
            K.NUMERIC,
            "Number of sites",
            "Number of listed trial sites.",
            ("LocationFacility",),
            False,
            _site_count,
        ),
    ]
}


def api_fields(dims: list[DimensionName]) -> str:
    """`fields=` parameter value covering the given dimensions."""
    fields = list(BASE_FIELDS)
    for d in dims:
        fields.extend(f for f in DIMENSIONS[d].api_fields if f not in fields)
    return "|".join(fields)
