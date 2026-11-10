"""Normalization of raw ClinicalTrials.gov values (README §7).

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
    r"\b(placebo|saline|sodium chloride|sham|vehicle|standard of care|best supportive care|usual care"
    r"|no intervention|control group|control arm|controls?)\b",
    re.IGNORECASE,
)
# Fragments left over after splitting registered names ('Drug 1 and Drug 2', 'X with the Y',
# 'Control Group' registered as DRUG) that are never drug names. Seen in a real unfiltered network.
_GENERIC_FRAGMENT = re.compile(
    r"^(the|a|an|of|or|in|on|to|for|arm|group|cohort|dose|doses|drug|drugs|treatment|therapy|regimen|"
    r"standard|experimental|active comparator|study drug|investigational product|intervention|"
    r"(?:drug|arm|group|cohort|dose|part|treatment|regimen|level|product) [a-z0-9]{1,3}|[a-z]?\d+[a-z]?|[ivx]+)$"
)
_PARTICLE_SIZE = re.compile(r"^\d+(?:\.\d+)?\s*-?\s*nm\s+")
_SALT_WORDS = {"sodium", "potassium", "chloride", "bromide", "citrate", "phosphate", "acetate", "sulfate"}

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


_SALT_SUFFIX = re.compile(
    r"\s+(hydrochloride|hcl|phosphate|sodium|potassium|citrate|mesylate|acetate|sulfate|tartrate|maleate"
    r"|succinate|fumarate|besylate|tosylate|disodium|dihydrochloride|monohydrate|bromide|chloride)$"
)
# MeSH annotates targets and classes next to the drug (e.g. 'CTLA-4 Antigen' for ipilimumab).
_MESH_TARGET = re.compile(r"\b(antigen|antigens|receptor|receptors|protein|proteins)$")
# Intervention MeSH terms also cover procedures and diagnostics (seen in real networks: 'Biopsy',
# 'Magnetic Resonance Imaging'). These words never name a drug.
_MESH_PROCEDURE = re.compile(
    r"\b(biopsy|imaging|tomography|ultrasonography|radiography|endoscopy|surgery|surgical|resection|"
    r"radiosurgery|radiotherapy|radiation|chemoradiotherapy|irradiation|brachytherapy|transplantation|dialysis|"
    r"specimen|blood sampling|questionnaires?|counseling|exercise|exergaming|diet|behavior therapy|psychotherapy|"
    r"electrocardiography|echocardiography|anesthesia|catheterization|laboratory|diagnostic|blood|"
    r"physical activity|training|education|rehabilitation|massage|yoga|meditation|debridement|"
    r"cryotherapy|hyperthermia|heating|stimulation|laparoscopy|as topic|drug evaluation|clinical trials?|"
    r"injections?|infusions?|drug administration)\b"
    r"|acupuncture"  # also 'electroacupuncture'
)
# Whole-term matches only: dosage forms, generic treatment words and stray MeSH descriptors.
_MESH_GENERIC = re.compile(
    r"^(tablets?|capsules?|solutions?|suspensions?|emulsions?|inhalation|topical|oral|placebos|methods|"
    r"therapeutics|chemotherapy|immunotherapy|neoadjuvant therapy|adjuvant therapy|palliative care|"
    r"standard treatment|conventional therapy|analgesia|quality of life|time|food|technology|pulse|air|"
    r"cd\d+[a-z]?)$"  # CD19, CD20: CAR-T / antibody targets
)

# WHO INN stems (https://www.who.int/teams/health-product-and-policy-standards/inn) and development
# codes. Used only to decide whether a registered name WITHOUT MeSH support is trusted as a drug.
_INN_STEM = re.compile(
    r"(mab|cept|nib|ciclib|parib|lisib|rafenib|platin|taxel|rubicin|tecan|trexate|citabine|uracil|mustine|"
    r"statin|fibrate|pril|sartan|olol|dipine|semide|thiazide|xaban|gatran|parin|grel|gliptin|gliflozin|"
    r"glutide|formin|prazole|tidine|setron|oxetine|triptyline|pramine|peridol|apine|azepam|zolam|caine|"
    r"profen|coxib|methasone|olone|nisone|lukast|terol|tropium|cillin|mycin|micin|cycline|floxacin|"
    r"conazole|azole|vir|vudine|leukin|kinra|ermin|lutamide|relix|rozole|estrant|tide|mide|zomib|lidomide)$"
)
_DRUG_CODE = re.compile(r"^[a-z]{1,8}[- ]?\d{2,}[a-z0-9-]*$")


def looks_like_drug(name: str) -> bool:
    """'pembrolizumab', 'hmpl-013', 'cba-1535' -> True; 'app', 'test', 'exercise' -> False."""
    key = text_key(name)
    return bool(_DRUG_CODE.match(key)) or any(bool(_INN_STEM.search(word)) for word in key.split())


def drug_key(name: str) -> str:
    """Grouping key for a drug: merges salt forms ('fludarabine phosphate' -> 'fludarabine')."""
    key = _PARTICLE_SIZE.sub("", text_key(name))  # '130-nm albumin-bound paclitaxel'
    while (stripped := _SALT_SUFFIX.sub("", key)) != key and stripped and stripped not in _SALT_WORDS:
        key = stripped
    return key


def is_generic_fragment(name: str) -> bool:
    key = text_key(name)
    return len(key) < 3 or bool(_GENERIC_FRAGMENT.match(key))


def is_mesh_non_drug(term: str) -> bool:
    key = text_key(term)
    # Inverted headings ('Receptors, Chimeric Antigen' = CAR therapy) are therapies, not targets.
    return (
        key in MESH_NON_DRUG
        or is_non_drug(key)
        or bool(_MESH_PROCEDURE.search(key))
        or bool(_MESH_GENERIC.match(key))
        or ("," not in key and bool(_MESH_TARGET.search(key)))
    )


def clean_drug_name(raw: str) -> str:
    """'Pembrolizumab 200 mg IV (MK-3475)' -> 'pembrolizumab'. Returns '' if nothing is left."""
    s = _TRADEMARK.sub("", raw)
    s = _PARENS.sub(" ", s)
    s = _DOSE.sub("", s)
    s = text_key(s).strip(" -,;:.")
    return _LEADING_ARTICLE.sub("", s)


_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")


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


_COMPANY_SUFFIX = re.compile(
    r"[,.]?\s+(inc|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|gmbh|ag|s\.?a|plc|b\.?v|n\.?v|s\.?p\.?a|"
    r"k\.?k|pty|lp)\.?$"
)


def organization_key(name: str) -> str:
    """Grouping key for sponsors: legal-form suffixes are dropped ('Celgene Corporation' = 'Celgene',
    'Daiichi Sankyo Co., Ltd.' = 'Daiichi Sankyo'); the name itself is untouched, so 'Merck Sharp &
    Dohme' and 'Merck KGaA' stay apart."""
    key = text_key(name)
    while (stripped := _COMPANY_SUFFIX.sub("", key).strip(" ,.")) != key and stripped:
        key = stripped
    return key


_TITLE_PREFIX = re.compile(r"^(dr|prof|professor|mr|mrs|ms)\.?\s+", re.IGNORECASE)


def person_name(raw: str) -> str:
    """'Sean van Diepen, MD, MSc' -> 'Sean van Diepen'; 'Prof. Helen McShane' -> 'Helen McShane'.
    Degrees and titles follow the first comma in registry entries."""
    return _TITLE_PREFIX.sub("", clean_label(raw.split(",")[0]))


# --- conditions ----------------------------------------------------------------

HEALTHY_KEY = "healthy volunteers"
HEALTHY_LABEL = "Healthy volunteers"
_HEALTHY = re.compile(
    r"^healthy( (adult|human|young|older|elderly|male|female))*( (volunteers?|participants?|subjects?|"
    r"adults?|individuals?|people|persons?|controls?|men|women))?$"
)


# Condition MeSH terms that ClinicalTrials.gov assigns wrongly: kept only if a free-text condition
# literally names them. Audit of 10000 trials: 'Agnosia' (a perception disorder) is attached to ~100
# pain / anesthesia trials, mostly from 'Analgesia' (pain relief), with no trial naming agnosia.
# String similarity cannot separate this from valid mappings (0.62 for both 'analgesia' -> 'agnosia'
# and 'depression' -> 'depressive disorder'), so such terms are listed explicitly.
# Value: free-text words to restore in place of the dropped term.
KNOWN_CONDITION_MISMAPS = {"agnosia": ("analgesia", "analgesic")}


def is_condition_mismap(mesh_term: str, raw_conditions: list[str]) -> bool:
    key = text_key(mesh_term)
    return key in KNOWN_CONDITION_MISMAPS and not any(key in text_key(raw) for raw in raw_conditions)


def restores_mismap(mesh_term: str, raw_condition: str) -> bool:
    """True if raw_condition is the free text a dropped mis-mapped term most likely came from."""
    return any(h in text_key(raw_condition) for h in KNOWN_CONDITION_MISMAPS.get(text_key(mesh_term), ()))


def is_healthy(condition: str) -> bool:
    """'Healthy', 'Healthy Volunteers', 'Healthy Adult Participants' -> True."""
    return bool(_HEALTHY.match(text_key(condition)))
