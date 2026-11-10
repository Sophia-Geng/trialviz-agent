"""HTTP API.

POST /visualize   question (+ optional structured fields) -> visualization spec
GET  /schema      JSON Schemas of the request, response and error bodies
GET  /health      liveness
GET  /demo        optional viewer that renders the specs (examples or live questions)
"""

from __future__ import annotations

import logging
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import TypeAdapter

from app.config import ROOT, settings
from app.ctgov.client import CtgovClient, UpstreamError
from app.pipeline import execute_plan
from app.planner.llm_client import OpenAIPlanner, PlannerError
from app.planner.llm_planner import Planner
from app.schemas.request import VisualizeRequest
from app.schemas.response import ErrorBody, ErrorResponse, VisualizeResponse
from app.validation.plan_rules import PlanRejected

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(
    title="ClinicalTrials.gov Query-to-Visualization Agent",
    version="1.0.0",
    description="Turns clinical-trial questions into visualization specs backed by ClinicalTrials.gov API v2.",
)


def _make_planner() -> Planner:
    try:
        return Planner(OpenAIPlanner())
    except PlannerError as exc:
        log.warning("Starting without LLM planner: %s", exc)
        return Planner(None)


app.state.ctgov = CtgovClient()
app.state.planner = _make_planner()


def _error(status: int, code: str, message: str, details: list | None = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details or []))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return _error(422, "VALIDATION_ERROR", "Request body is invalid.", details)


ERROR_RESPONSES = {
    422: {"model": ErrorResponse, "description": "Invalid request, or a plan that cannot be executed."},
    502: {"model": ErrorResponse, "description": "ClinicalTrials.gov unavailable."},
    501: {"model": ErrorResponse, "description": "Visualization type not implemented."},
}


@app.post("/visualize", response_model=VisualizeResponse, responses=ERROR_RESPONSES)
def visualize(body: VisualizeRequest, request: Request):
    try:
        plan = request.app.state.planner.plan(body)
        return execute_plan(plan, request.app.state.ctgov)
    except PlanRejected as exc:
        return _error(422, "PLAN_ERROR", "The question and the explicit fields lead to an impossible plan.", exc.errors)
    except UpstreamError as exc:
        return _error(502, "UPSTREAM_ERROR", str(exc))
    except NotImplementedError as exc:
        return _error(501, "PLAN_ERROR", str(exc))


@app.get("/schema")
def schema() -> dict:
    return {
        "request": VisualizeRequest.model_json_schema(),
        "response": TypeAdapter(VisualizeResponse).json_schema(),
        "error": ErrorResponse.model_json_schema(),
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_model": settings.openai_model if settings.openai_api_key else None}


# --- demo viewer (not part of the API contract) -------------------------------

DEMO_PAGE = ROOT / "demo" / "index.html"
EXAMPLES_DIR = ROOT / "examples"
_EXAMPLE_NAME = re.compile(r"^[\w-]+$")


@app.get("/demo", include_in_schema=False)
def demo() -> FileResponse:
    return FileResponse(DEMO_PAGE)


@app.get("/demo/examples", include_in_schema=False)
def list_examples() -> list[str]:
    return sorted(p.stem for p in EXAMPLES_DIR.glob("*.json"))


@app.get("/demo/examples/{name}", include_in_schema=False)
def get_example(name: str) -> FileResponse:
    path = EXAMPLES_DIR / f"{name}.json"
    if not _EXAMPLE_NAME.match(name) or not path.is_file():
        raise HTTPException(status_code=404, detail="example not found")
    return FileResponse(path)
