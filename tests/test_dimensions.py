import pytest

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
    assert keys(DimensionName.DRUG) == []  # only an OTHER-type intervention (palliative care)


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
    for s in studies:
        keys = {v.key for v in DIMENSIONS[DimensionName.DRUG].extract(s)}
        hits += drug in keys
        assert not synonyms & keys, dims.nct_id(s)
    assert hits / len(studies) >= 0.95


def test_drug_excludes_placebo_and_procedures(all_studies):
    for study in all_studies:
        for v in DIMENSIONS[DimensionName.DRUG].extract(study):
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
    (v,) = DIMENSIONS[DimensionName.DRUG].extract(study)
    assert (v.key, v.excerpt, v.field_path) == (
        "foo-123", "Foo-123 10 mg", "protocolSection.armsInterventionsModule.interventions[0].name",
    )


def test_drug_fallback_splits_combinations():
    study = {
        "protocolSection": {
            "armsInterventionsModule": {
                "interventions": [{"type": "DRUG", "name": "CBA-1535"}, {"type": "DRUG", "name": "CBA-1535+Pembrolizumab"}]
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
    assert [v.label for v in DIMENSIONS[DimensionName.INVESTIGATOR].extract(study)] == ["Jane Doe, MD (Mayo Clinic)"]


def test_api_fields_dedupes_and_includes_base():
    fields = dims.api_fields([DimensionName.START_YEAR, DimensionName.DURATION_MONTHS])
    assert fields == "NCTId|BriefTitle|StartDate|StartDateType|CompletionDate|CompletionDateType"
