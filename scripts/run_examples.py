"""Generate examples/: real requests sent through the HTTP API (LLM planner + live ClinicalTrials.gov).

Usage:
    python scripts/run_examples.py          # all examples
    python scripts/run_examples.py network  # examples whose file name contains "network"

Requires OPENAI_API_KEY in .env. Responses are written verbatim, together with the request.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "examples"

EXAMPLES: dict[str, dict] = {
    "01_time_series_drug_trend": {
        "query": "How has the number of trials for this drug changed per year since 2015?",
        "drug_name": "Pembrolizumab",
    },
    "02_bar_phase_distribution": {
        "query": "How are Alzheimer's disease trials distributed across phases?",
    },
    "03_grouped_bar_drug_comparison": {
        "query": "Compare phases for trials involving Pembrolizumab vs Nivolumab.",
    },
    "04_bar_recruiting_countries": {
        "query": "Which countries have the most recruiting trials for breast cancer?",
    },
    "05_network_sponsor_drug": {
        "query": "Show a network of sponsors ↔ drugs for melanoma trials.",
    },
    "06_network_drug_combinations": {
        "query": "Which drugs frequently co-occur with nivolumab in combination studies?",
    },
    "07_metric_single_value": {
        "query": "How many trials are currently recruiting for ALS?",
    },
    "08_unsupported_question": {
        "query": "Is pembrolizumab safe for pregnant women?",
    },
}


def main(name_filter: str | None) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    client = TestClient(app)
    for name, request in EXAMPLES.items():
        if name_filter and name_filter not in name:
            continue
        start = time.time()
        resp = client.post("/visualize", json=request)
        elapsed = time.time() - start
        body = resp.json()
        (OUT_DIR / f"{name}.json").write_text(
            json.dumps({"request": request, "status_code": resp.status_code, "response": body}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        viz = body.get("visualization") or {}
        meta = body.get("meta", {})
        print(f"{name:34s} {resp.status_code} {viz.get('type', '-'):18s} {elapsed:5.1f}s  {viz.get('title', body.get('error', ''))}")
        for note in meta.get("notes", []):
            print(f"{'':36s}note: {note}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
