"""ClinicalTrials.gov API v2 client: paging, rate limiting, retries and a file cache.

The API has no documented rate limit, but ~10 requests in 5 s returns 429, so
requests are serialized with a minimum interval and 429 / 5xx are retried with
backoff (honoring Retry-After). Responses are cached on disk keyed by path +
params, which makes example runs reproducible and keeps development off the API.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import settings

PAGE_SIZE_MAX = 1000


class UpstreamError(RuntimeError):
    """ClinicalTrials.gov could not be reached or returned an error."""


@dataclass
class FetchResult:
    studies: list[dict[str, Any]]
    total: int
    retrieved_at: datetime
    pages: int = 0

    @property
    def truncated(self) -> bool:
        return len(self.studies) < self.total


@dataclass
class _RateLimiter:
    min_interval: float
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last: float = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class CtgovClient:
    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        base_url: str = settings.ctgov_base_url,
        cache_dir: Path | None = settings.cache_dir / "ctgov" if settings.cache_enabled else None,
        min_interval: float = settings.ctgov_min_interval_s,
        max_retries: int = 4,
        sleep=time.sleep,
    ) -> None:
        self._http = http or httpx.Client(timeout=settings.ctgov_timeout_s, headers={"Accept": "application/json"})
        self._base_url = base_url.rstrip("/")
        self._cache_dir = cache_dir
        self._limiter = _RateLimiter(min_interval)
        self._max_retries = max_retries
        self._sleep = sleep

    # --- public API ----------------------------------------------------------

    def count(self, params: dict[str, str]) -> tuple[int, datetime]:
        """Exact number of studies matching `params` (one request)."""
        body, retrieved_at = self._get("/studies", {**params, "countTotal": "true", "pageSize": "1", "fields": "NCTId"})
        return int(body.get("totalCount", 0)), retrieved_at

    def fetch_studies(self, params: dict[str, str], fields: str, max_records: int) -> FetchResult:
        """Page through /studies until exhausted or `max_records` studies are fetched."""
        base = {**params, "fields": fields}
        page_params = {**base, "countTotal": "true", "pageSize": str(min(PAGE_SIZE_MAX, max_records))}
        studies: list[dict] = []
        total = 0
        first_retrieved: datetime | None = None
        pages = 0
        while True:
            body, retrieved_at = self._get("/studies", page_params)
            pages += 1
            first_retrieved = first_retrieved or retrieved_at
            if pages == 1:
                total = int(body.get("totalCount", 0))
            studies.extend(body.get("studies", []))
            token = body.get("nextPageToken")
            remaining = max_records - len(studies)
            if not token or remaining <= 0:
                break
            # Later pages repeat every parameter except countTotal (spec), plus the token.
            page_params = {**base, "pageSize": str(min(PAGE_SIZE_MAX, remaining)), "pageToken": token}
        return FetchResult(studies[:max_records], total, first_retrieved, pages)

    def version(self) -> dict[str, Any]:
        body, _ = self._get("/version", {})
        return body

    # --- internals -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> tuple[dict, datetime]:
        cache_file = self._cache_path(path, params)
        if cache_file and cache_file.exists():
            entry = json.loads(cache_file.read_text(encoding="utf-8"))
            return entry["body"], datetime.fromisoformat(entry["retrieved_at"])

        body = self._request(path, params)
        retrieved_at = datetime.now(UTC)
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"retrieved_at": retrieved_at.isoformat(), "body": body}), encoding="utf-8"
            )
        return body, retrieved_at

    def _request(self, path: str, params: dict[str, str]) -> dict:
        url = self._base_url + path
        last_error = ""
        for attempt in range(self._max_retries + 1):
            self._limiter.wait()
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as exc:  # timeouts, connection resets
                last_error = f"{type(exc).__name__}: {exc}"
                self._sleep(2.0 * 2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else 2.0 * 2**attempt)
                continue
            if resp.status_code >= 400:
                raise UpstreamError(f"ClinicalTrials.gov returned HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise UpstreamError(f"ClinicalTrials.gov unavailable after {self._max_retries + 1} attempts ({last_error})")

    def _cache_path(self, path: str, params: dict[str, str]) -> Path | None:
        if self._cache_dir is None:
            return None
        key = json.dumps([path, sorted(params.items())], ensure_ascii=False)
        return self._cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"
