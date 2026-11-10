import pytest

from app.aggregate.engine import extract_batch
from app.data import dimensions as dims
from app.data.dimensions import DIMENSIONS, MISSING_EXCERPT
from app.data.normalize import UNKNOWN
from app.schemas.enums import DimensionKind, DimensionName
from tests.conftest import load_fixture, load_studies


def test_registry_covers_every_dimension_name():
    assert set(DIMENSIONS) == set(DimensionName)


@pytest.mark.parametrize("name", list(DimensionName))
def test_every_dimension_extracts_on_real_data(name, all_studies):
    dim = DIMENSIONS[name]
    for study in all_studies:
        values = dim.extract(study)
        keys = [v.key for v in values]
        assert len(keys) == len(set(keys)), "a trial must count once per distinct value"
        if not dim.multi_valued:
            assert len(values) <= 1
        for v in values:
            assert v.excerpt and v.field_path.startswith(("protocolSection", "derivedSection"))
            if dim.kind is DimensionKind.CATEGORICAL:
                assert isinstance(v.key, str) and v.label
            else:
                assert isinstance(v.key, float)


def test_single_study_full_record():
    study = load_fixture("study_NCT03540771_full")["pages"][0]
    assert dims.nct_id(study) == "NCT03540771"

    def keys(name):
        return [v.key for v in DIMENSIONS[name].extract(study)]

    assert keys(DimensionName.PHASE) == ["Not Applicable"]
    assert keys(DimensionName.START_YEAR) == [2019.0]
    assert keys(DimensionName.OVERALL_STATUS) == ["COMPLETED"]
    assert keys(DimensionName.SPONSOR_CLASS) == ["OTHER"]
    assert keys(DimensionName.ENROLLMENT) == [1494.0]
    assert keys(DimensionName.DURATION_MONTHS) == [79.0]
    assert keys(DimensionName.COUNTRY) == ["united states"]  # many US sites, counted once
    # only an OTHER-type intervention (palliative care), which matches no confirmed drug
    assert extract_batch([study], DimensionName.DRUG) == [[]]


def test_phase_excerpt_is_raw_api_value():
    study = load_fixture("study_NCT03540771_full")["pages"][0]
    (v,) = DIMENSIONS[DimensionName.PHASE].extract(study)
    assert v.excerpt == '["NA"]'
    assert v.field_path == "protocolSection.designModule.phases"


def test_missing_categorical_goes_to_unknown():
    study = {"protocolSection": {"identificationModule": {"nctId": "NCT00000000"}}}
    for name in (DimensionName.PHASE, DimensionName.LEAD_SPONSOR, DimensionName.CONDITION, DimensionName.COUNTRY):
        (v,) = DIMENSIONS[name].extract(study)
        assert v.key == UNKNOWN and v.excerpt == MISSING_EXCERPT
    for name in (DimensionName.START_YEAR, DimensionName.ENROLLMENT, DimensionName.DURATION_MONTHS):
        assert DIMENSIONS[name].extract(study) == []


@pytest.mark.parametrize(
    ("fixture", "drug", "synonyms"),
    [
        ("pembrolizumab_since_2015", "pembrolizumab", {"mk-3475", "mk3475", "keytruda", "pembrolizumab keytruda"}),
        ("nivolumab", "nivolumab", {"bms-936558", "opdivo"}),
    ],
)
def test_drug_mesh_merges_synonyms(fixture, drug, synonyms):
    """Every trial matched by the strict drug search maps to one drug key; synonyms never form their own key.

    MeSH occasionally misses a drug inside a combination name (e.g. 'CBA-1535+Pembrolizumab'),
    so allow a few gaps. Cohort counts come from the API search itself and are unaffected.
    """
    studies = load_studies(fixture)
    hits = 0
    for s, values in zip(studies, extract_batch(studies, DimensionName.DRUG), strict=True):
        keys = {v.key for v in values}
        hits += drug in keys
        assert not synonyms & keys, dims.nct_id(s)
    assert hits / len(studies) >= 0.95


def test_non_drug_type_regimen_resolved_against_batch():
    """NCT02704156 registers 'Cyberknife plus Pembrolizumab and Trametinib' as DEVICE."""

    def trial(nid, interventions, mesh=()):
        return {
            "protocolSection": {
                "identificationModule": {"nctId": nid},
                "armsInterventionsModule": {"interventions": interventions},
            },
            "derivedSection": {"interventionBrowseModule": {"meshes": [{"term": t} for t in mesh]}},
        }

    batch = [
        trial("NCT1", [{"type": "DRUG", "name": "MK-3475"}], mesh=["pembrolizumab"]),
        trial("NCT2", [{"type": "DEVICE", "name": "Cyberknife plus Pembrolizumab and Trametinib"}]),
        trial("NCT3", [{"type": "OTHER", "name": "Morning pembrolizumab infusions"}]),
        trial("NCT4", [{"type": "DEVICE", "name": "Insulin pump"}]),
    ]
    resolved = [
        [(v.key, v.excerpt, v.tentative) for v in values] for values in extract_batch(batch, DimensionName.DRUG)
    ]
    assert resolved[1] == [("pembrolizumab", "Cyberknife plus Pembrolizumab and Trametinib", False)]
    assert resolved[2] == [("pembrolizumab", "Morning pembrolizumab infusions", False)]
    assert resolved[3] == []  # 'insulin pump' matches no confirmed drug, 'cyberknife'/'trametinib' never confirmed


def test_drug_excludes_placebo_and_procedures(all_studies):
    for values in extract_batch(all_studies, DimensionName.DRUG):
        for v in values:
            assert "placebo" not in v.key
            assert v.key not in {"radiotherapy", "drug therapy", "specimen handling"}


def test_drug_falls_back_to_cleaned_name_without_mesh():
    study = {
        "protocolSection": {
            "armsInterventionsModule": {
                "interventions": [
                    {"type": "DRUG", "name": "Foo-123 10 mg"},
                    {"type": "DRUG", "name": "Placebo"},
                    {"type": "DEVICE", "name": "Pump"},
                ]
            }
        }
    }
    ((v,),) = extract_batch([study], DimensionName.DRUG)  # the DEVICE 'Pump' matches no drug
    assert (v.key, v.excerpt, v.field_path) == (
        "foo-123",
        "Foo-123 10 mg",
        "protocolSection.armsInterventionsModule.interventions[0].name",
    )


def test_condition_prefers_mesh_and_merges_spellings():
    def trial(conditions, mesh=()):
        return {
            "protocolSection": {"conditionsModule": {"conditions": conditions}},
            "derivedSection": {"conditionBrowseModule": {"meshes": [{"id": "D", "term": t} for t in mesh]}},
        }

    batch = [
        trial(["Type 2 Diabetes"], mesh=["Diabetes Mellitus, Type 2"]),
        trial(["T2DM", "Obesity"], mesh=["Diabetes Mellitus, Type 2", "Obesity"]),
        trial(["Healthy Volunteers"]),
        trial(["Healthy Adult Participants"]),
    ]
    keys = [[v.key for v in vs] for vs in extract_batch(batch, DimensionName.CONDITION)]
    assert keys == [
        ["diabetes mellitus, type 2"],
        ["diabetes mellitus, type 2", "obesity"],
        ["healthy volunteers"],
        ["healthy volunteers"],
    ]


@pytest.mark.parametrize(
    ("fixture", "mesh_term", "spellings"),
    [
        (
            "alzheimer",
            "alzheimer disease",
            {"alzheimer's disease", "alzheimer disease", "alzheimers disease", "alzheimer's"},
        ),
        ("melanoma", "melanoma", {"melanoma", "malignant melanoma", "metastatic melanoma"}),
    ],
)
def test_condition_mesh_merges_spellings_on_real_data(fixture, mesh_term, spellings):
    """Trials searched by condition map onto one MeSH heading; raw spellings never form their own value."""
    studies = load_studies(fixture)
    values = extract_batch(studies, DimensionName.CONDITION)
    hits = sum(mesh_term in {v.key for v in vs} for vs in values)
    assert hits / len(studies) >= 0.8
    raw_only = {v.key for vs in values for v in vs if "conditionsModule" in v.field_path}
    assert not (spellings - {mesh_term}) & raw_only


def test_condition_known_mesh_mismap_falls_back_to_free_text():
    """ClinicalTrials.gov maps 'Analgesia' to MeSH 'Agnosia' (a perception disorder)."""
    study = {
        "protocolSection": {"conditionsModule": {"conditions": ["Analgesia", "Postoperative Pain"]}},
        "derivedSection": {
            "conditionBrowseModule": {
                "meshes": [{"id": "D000377", "term": "Agnosia"}, {"id": "D010149", "term": "Pain, Postoperative"}]
            }
        },
    }
    ((*values,),) = extract_batch([study], DimensionName.CONDITION)
    assert [(v.key, v.excerpt) for v in values] == [
        ("pain, postoperative", "Pain, Postoperative"),
        ("analgesia", "Analgesia"),
    ]


def test_drug_fallback_drops_fragments_and_controls():
    study = {
        "protocolSection": {
            "armsInterventionsModule": {
                "interventions": [
                    {"type": "DRUG", "name": "Drug 1 and Drug 2"},
                    {"type": "DRUG", "name": "Control Group"},
                    {"type": "DRUG", "name": "Sodium Chloride 0.9%"},
                    {"type": "DRUG", "name": "Aspirin with the Clopidogrel"},
                ]
            }
        }
    }
    ((*values,),) = extract_batch([study], DimensionName.DRUG)
    # 'clopidogrel' has an INN stem (-grel); 'aspirin' has none, so on its own it stays unconfirmed
    assert [v.key for v in values] == ["clopidogrel"]

    # ... and is kept as soon as another trial in the batch confirms it through MeSH
    confirming = {
        "protocolSection": {"armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "ASA"}]}},
        "derivedSection": {"interventionBrowseModule": {"meshes": [{"id": "D001241", "term": "Aspirin"}]}},
    }
    values = extract_batch([study, confirming], DimensionName.DRUG)[0]
    assert {v.key for v in values} == {"clopidogrel", "aspirin"}


def test_mesh_terms_need_evidence_in_mixed_trials():
    """MeSH terms belong to the whole trial; a behavioral arm must not turn 'Mindfulness' into a drug."""
    study = {
        "protocolSection": {
            "armsInterventionsModule": {
                "interventions": [
                    {"type": "DRUG", "name": "MK-3475", "otherNames": ["pembrolizumab", "Keytruda"]},
                    {"type": "DRUG", "name": "Carboplatin AUC 5"},
                    {"type": "BEHAVIORAL", "name": "Mindfulness program"},
                ]
            }
        },
        "derivedSection": {
            "interventionBrowseModule": {
                "meshes": [
                    {"id": "C582435", "term": "pembrolizumab"},  # chemical concept id
                    {"id": "D016190", "term": "Carboplatin"},  # matches a drug intervention name
                    {"id": "D000075702", "term": "Mindfulness"},  # descriptor from the behavioral arm
                ]
            }
        },
    }
    ((*values,),) = extract_batch([study], DimensionName.DRUG)
    assert [v.key for v in values] == ["pembrolizumab", "carboplatin"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("pembrolizumab", True),
        ("hmpl-013", True),
        ("cba-1535", True),
        ("trastuzumab deruxtecan", True),
        ("metformin", True),
        ("app", False),
        ("test", False),
        ("exercise", False),
        ("mindfulness", False),
    ],
)
def test_looks_like_drug(name, expected):
    from app.data.normalize import looks_like_drug

    assert looks_like_drug(name) is expected


def test_drug_fallback_splits_combinations():
    study = {
        "protocolSection": {
            "armsInterventionsModule": {
                "interventions": [
                    {"type": "DRUG", "name": "CBA-1535"},
                    {"type": "DRUG", "name": "CBA-1535+Pembrolizumab"},
                ]
            }
        }
    }
    values = DIMENSIONS[DimensionName.DRUG].extract(study)
    assert [v.key for v in values] == ["cba-1535", "pembrolizumab"]
    assert values[1].excerpt == "CBA-1535+Pembrolizumab"  # citation keeps the raw combined name


def _study_with_locations(overall_status, locations):
    return {
        "protocolSection": {
            "statusModule": {"overallStatus": overall_status},
            "contactsLocationsModule": {"locations": locations},
        }
    }


def test_recruiting_country_uses_site_status():
    study = _study_with_locations(
        "RECRUITING",
        [
            {"country": "France", "status": "RECRUITING"},
            {"country": "Spain", "status": "NOT_YET_RECRUITING"},
            {"country": "France", "status": "RECRUITING"},
        ],
    )
    assert [v.key for v in DIMENSIONS[DimensionName.RECRUITING_COUNTRY].extract(study)] == ["france"]


def test_recruiting_country_falls_back_to_overall_status():
    locations = [{"country": "Japan"}, {"country": "Japan"}]
    recruiting = _study_with_locations("RECRUITING", locations)
    completed = _study_with_locations("COMPLETED", locations)
    assert [v.key for v in DIMENSIONS[DimensionName.RECRUITING_COUNTRY].extract(recruiting)] == ["japan"]
    assert DIMENSIONS[DimensionName.RECRUITING_COUNTRY].extract(completed) == []


def test_site_and_investigator_drop_placeholders():
    study = {
        "protocolSection": {
            "contactsLocationsModule": {
                "locations": [{"facility": "Research Site"}, {"facility": "Mayo Clinic"}],
                "overallOfficials": [
                    {"name": "Medical Director", "role": "STUDY_DIRECTOR", "affiliation": "Merck"},
                    {"name": "Jane Doe, MD", "role": "PRINCIPAL_INVESTIGATOR", "affiliation": "Mayo Clinic"},
                ],
            }
        }
    }
    assert [v.label for v in DIMENSIONS[DimensionName.SITE].extract(study)] == ["Mayo Clinic"]
    assert [v.label for v in DIMENSIONS[DimensionName.INVESTIGATOR].extract(study)] == ["Jane Doe (Mayo Clinic)"]


def test_api_fields_dedupes_and_includes_base():
    fields = dims.api_fields([DimensionName.START_YEAR, DimensionName.DURATION_MONTHS])
    assert fields == "NCTId|BriefTitle|StartDate|StartDateType|CompletionDate|CompletionDateType"
