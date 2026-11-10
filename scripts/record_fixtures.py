"""Record real ClinicalTrials.gov API v2 responses into tests/fixtures/.

Each fixture stores the exact request (path + params), the retrieval time and
the raw JSON response, so tests run offline against real-world data shapes.

Usage:
    python scripts/record_fixtures.py            # record all
    python scripts/record_fixtures.py melanoma   # record fixtures whose name contains "melanoma"
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

BASE_URL = "https://clinicaltrials.gov/api/v2"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Piece names covering every dimension in app/data/dimensions.py (verified against the live API).
FIELDS = "|".join(
    [
        "NCTId",
        "BriefTitle",
        "Phase",
        "StartDate",
        "StartDateType",
        "CompletionDate",
        "CompletionDateType",
        "OverallStatus",
        "StudyType",
        "LeadSponsorName",
        "LeadSponsorClass",
        "InterventionType",
        "InterventionName",
        "InterventionOtherName",
        "InterventionMeshTerm",
        "InterventionMeshId",
        "Condition",
        "ConditionMeshTerm",
        "LocationCountry",
        "LocationStatus",
        "LocationFacility",
        "OverallOfficialName",
        "OverallOfficialRole",
        "OverallOfficialAffiliation",
        "EnrollmentCount",
    ]
)


def drug_query(name: str) -> str:
    """Strict drug match; plain query.intr applies concept expansion and over-matches (README §6.3)."""
    return f"AREA[InterventionName]{name} OR AREA[InterventionMeshTerm]{name}"


# name -> (path, params, pages). One fixture per example query in README §9,
# plus metadata endpoints, a full single record and a multi-page sample.
FIXTURES: dict[str, tuple[str, dict[str, str | int], int]] = {
    "enums": ("/studies/enums", {}, 1),
    "version": ("/version", {}, 1),
    "study_NCT03540771_full": ("/studies/NCT03540771", {}, 1),
    "pembrolizumab_since_2015": (
        "/studies",
        {
            "query.intr": drug_query("pembrolizumab"),
            "filter.advanced": "AREA[StartDate]RANGE[2015-01-01,MAX]",
            "fields": FIELDS,
            "pageSize": 100,
            "countTotal": "true",
        },
        1,
    ),
    "nivolumab": (
        "/studies",
        {"query.intr": drug_query("nivolumab"), "fields": FIELDS, "pageSize": 100, "countTotal": "true"},
        1,
    ),
    "alzheimer": (
        "/studies",
        {"query.cond": "Alzheimer's disease", "fields": FIELDS, "pageSize": 100, "countTotal": "true"},
        1,
    ),
    "breast_cancer_recruiting": (
        "/studies",
        {
            "query.cond": "breast cancer",
            "filter.overallStatus": "RECRUITING",
            "fields": FIELDS,
            "pageSize": 100,
            "countTotal": "true",
        },
        1,
    ),
    "melanoma": (
        "/studies",
        {"query.cond": "melanoma", "fields": FIELDS, "pageSize": 100, "countTotal": "true"},
        1,
    ),
    # Small pages to exercise nextPageToken handling.
    "paging_lung_cancer_recruiting": (
        "/studies",
        {
            "query.cond": "lung cancer",
            "filter.overallStatus": "RECRUITING",
            "fields": "NCTId|BriefTitle|Phase",
            "pageSize": 5,
            "countTotal": "true",
        },
        3,
    ),
}


def get_with_retry(client: httpx.Client, url: str, params: dict, attempts: int = 5) -> httpx.Response:
    """GET with backoff on 429/5xx, honoring Retry-After when present."""
    for attempt in range(attempts):
        resp = client.get(url, params=params)
        if resp.status_code != 429 and resp.status_code < 500:
            resp.raise_for_status()
            return resp
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * 2**attempt
        print(f"  HTTP {resp.status_code}, retry-after={retry_after!r}, sleeping {delay:.0f}s")
        time.sleep(delay)
    resp.raise_for_status()
    return resp


def fetch(client: httpx.Client, path: str, params: dict, pages: int) -> list[dict]:
    """Fetch up to `pages` pages, following nextPageToken."""
    responses = []
    page_params = dict(params)
    for _ in range(pages):
        resp = get_with_retry(client, BASE_URL + path, page_params)
        body = resp.json()
        responses.append(body)
        token = body.get("nextPageToken") if isinstance(body, dict) else None
        if not token:
            break
        # Later pages must repeat all params except countTotal / pageToken.
        page_params = {k: v for k, v in params.items() if k != "countTotal"}
        page_params["pageToken"] = token
        time.sleep(0.5)
    return responses


def main(name_filter: str | None) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30.0, headers={"Accept": "application/json"}) as client:
        for name, (path, params, pages) in FIXTURES.items():
            if name_filter and name_filter not in name:
                continue
            responses = fetch(client, path, params, pages)
            fixture = {
                "request": {"path": path, "params": params},
                "retrieved_at": datetime.now(UTC).isoformat(),
                "pages": responses,
            }
            out = FIXTURE_DIR / f"{name}.json"
            out.write_text(json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8")
            first = responses[0]
            summary = (
                f"totalCount={first.get('totalCount')} studies={sum(len(r.get('studies', [])) for r in responses)}"
                if isinstance(first, dict) and "studies" in first
                else "metadata"
            )
            print(f"{name:32s} pages={len(responses)} {summary} -> {out.name} ({out.stat().st_size // 1024} KB)")
            time.sleep(0.5)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
