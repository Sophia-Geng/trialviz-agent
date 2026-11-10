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


def test_text_key_merges_case_and_whitespace():
    assert n.text_key("  Breast   Cancer ") == n.text_key("breast cancer")
