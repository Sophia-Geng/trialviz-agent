from datetime import date

import pytest

from app.data import normalize as n


@pytest.mark.parametrize(
    ("phases", "expected"),
    [
        (["PHASE1"], "Phase 1"),
        (["EARLY_PHASE1"], "Early Phase 1"),
        (["PHASE1", "PHASE2"], "Phase 1/2"),
        (["PHASE2", "PHASE1"], "Phase 1/2"),
        (["PHASE2", "PHASE3"], "Phase 2/3"),
        (["PHASE4"], "Phase 4"),
        (["NA"], "Not Applicable"),
        ([], "Unknown"),
        (None, "Unknown"),
        (["SOMETHING_NEW"], "Unknown"),
    ],
)
def test_normalize_phase(phases, expected):
    assert n.normalize_phase(phases) == expected


def test_phase_order_is_total():
    labels = ["Unknown", "Phase 3", "Early Phase 1", "Phase 1/2", "Not Applicable", "Phase 1/2/3"]
    assert sorted(labels, key=n.phase_sort_key) == [
        "Early Phase 1", "Phase 1/2", "Phase 3", "Phase 1/2/3", "Not Applicable", "Unknown",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2019-01-30", date(2019, 1, 30)),
        ("2019-01", date(2019, 1, 1)),
        ("2019", date(2019, 1, 1)),
        (" 2019-01 ", date(2019, 1, 1)),
        ("2019-13", None),
        ("January 2019", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(raw, expected):
    assert n.parse_date(raw) == expected


def test_months_between():
    assert n.months_between(date(2019, 1, 30), date(2025, 8, 30)) == 79.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Pembrolizumab", "pembrolizumab"),
        ("Pembrolizumab KEYTRUDA®", "pembrolizumab keytruda"),
        ("Pembrolizumab 200 mg IV", "pembrolizumab"),
        ("Nivolumab (BMS-936558)", "nivolumab"),
        ("DK210 (EGFR)", "dk210"),
        ("5-fluorouracil-based chemotherapy", "5-fluorouracil-based chemotherapy"),
        ("Cisplatin 75 mg/m2", "cisplatin"),
        ("  MK3475  ", "mk3475"),
    ],
)
def test_clean_drug_name(raw, expected):
    assert n.clean_drug_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CBA-1535+Pembrolizumab", ["CBA-1535", "Pembrolizumab"]),
        ("Gemcitabine+ nivolumab", ["Gemcitabine", "nivolumab"]),
        ("Nivolumab Combined with Anlotinib", ["Nivolumab", "Anlotinib"]),
        ("Lenvatinib plus Pembrolizumab", ["Lenvatinib", "Pembrolizumab"]),
        ("IO102/IO103", ["IO102", "IO103"]),
        ("5-fluorouracil", ["5-fluorouracil"]),
        ("Nivolumab-relatlimab FDC", ["Nivolumab-relatlimab FDC"]),
    ],
)
def test_split_combination(raw, expected):
    assert n.split_combination(raw) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Placebo", True),
        ("placebo Administration", True),
        ("best supportive care (BSC)", True),
        ("Normal saline", True),
        ("Pembrolizumab", False),
    ],
)
def test_is_non_drug(name, expected):
    assert n.is_non_drug(name) is expected


@pytest.mark.parametrize(
    ("facility", "expected"),
    [
        ("Research Site", True),
        ("Novartis Investigative Site", True),
        ("Local Institution - 0012", True),
        ("Site 101", True),
        ("Yale New Haven Hospital", False),
        ("Massey Cancer Center", False),
    ],
)
def test_is_placeholder_site(facility, expected):
    assert n.is_placeholder_site(facility) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Medical Director", True),
        ("Pfizer CT.gov Call Center", True),
        ("Medical Officer", False),  # kept: name-like enough; filtered by role instead when needed
        ("Barbara Burtness, MD, Prof", False),
    ],
)
def test_is_placeholder_official(name, expected):
    assert n.is_placeholder_official(name) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Fludarabine Phosphate", "fludarabine"),
        ("Erlotinib Hydrochloride", "erlotinib"),
        ("Gemcitabine Hydrochloride", "gemcitabine"),
        ("Pemetrexed Disodium", "pemetrexed"),
        ("sodium chloride", "sodium chloride"),  # never reduced to a bare salt word
        ("Pembrolizumab", "pembrolizumab"),
        ("130-nm albumin-bound paclitaxel", "albumin-bound paclitaxel"),
    ],
)
def test_drug_key_merges_salt_forms(name, expected):
    assert n.drug_key(name) == expected


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("CTLA-4 Antigen", True), ("Receptors, Chimeric Antigen", False), ("Radiotherapy", True),
        ("Biopsy", True), ("Magnetic Resonance Imaging", True), ("Positron-Emission Tomography", True),
        ("Nivolumab", False), ("130-nm albumin-bound paclitaxel", False), ("Cyclophosphamide", False),
    ],
)
def test_is_mesh_non_drug(term, expected):
    assert n.is_mesh_non_drug(term) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("the", True), ("1", True), ("2b", True), ("arm", True), ("part a", True), ("ii", True), ("5", True),
     ("ab", True), ("aspirin", False), ("5-fluorouracil", False), ("abc-123", False)],
)
def test_is_generic_fragment(name, expected):
    assert n.is_generic_fragment(name) is expected


@pytest.mark.parametrize(
    ("term", "expected"),
    [("Control Group", True), ("Saline Solution", True), ("Sodium Chloride", True), ("Exercise", True),
     ("Injections", True), ("Surgery", True), ("Venetoclax", False), ("Azacitidine", False),
     ("Blood Specimen Collection", True), ("Electroacupuncture", True), ("Transcranial Magnetic Stimulation", True),
     ("Tablets", True), ("CD19", True), ("Quality of Life", True),
     ("Oral Glucose", False), ("Botulinum Toxins, Type A", False), ("Oxygen", False), ("Probiotics", False)],
)
def test_mesh_controls_and_saline_are_not_drugs(term, expected):
    assert n.is_mesh_non_drug(term) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Sean van Diepen, MD", "Sean van Diepen"), ("Sean van Diepen, MD, MSc", "Sean van Diepen"),
     ("Ali MA Ismail, lecturer", "Ali MA Ismail"), ("Prof. Helen McShane", "Helen McShane"), ("Lin Shen", "Lin Shen")],
)
def test_person_name_drops_degrees_and_titles(raw, expected):
    assert n.person_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Healthy", True), ("Healthy Volunteers", True), ("healthy participants", True), ("Healthy Adult Subjects", True),
     ("Healthy Aging", False), ("Healthy Lifestyle", False), ("Breast Cancer", False)],
)
def test_is_healthy(raw, expected):
    assert n.is_healthy(raw) is expected


def test_text_key_merges_case_and_whitespace():
    assert n.text_key("  Breast   Cancer ") == n.text_key("breast cancer")
