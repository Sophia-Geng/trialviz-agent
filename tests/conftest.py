import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
STUDY_FIXTURES = ["pembrolizumab_since_2015", "nivolumab", "alzheimer", "breast_cancer_recruiting", "melanoma"]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def load_studies(name: str) -> list[dict]:
    return [s for page in load_fixture(name)["pages"] for s in page["studies"]]


@pytest.fixture(scope="session")
def all_studies() -> list[dict]:
    return [s for name in STUDY_FIXTURES for s in load_studies(name)]
