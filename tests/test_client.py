import httpx
import pytest

from app.ctgov.client import CtgovClient, UpstreamError


def _client(handler, tmp_path=None, **kw) -> CtgovClient:
    return CtgovClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://example.test/api/v2",
        cache_dir=tmp_path,
        min_interval=0,
        sleep=lambda s: None,
        **kw,
    )


def _study(i: int) -> dict:
    return {"protocolSection": {"identificationModule": {"nctId": f"NCT{i:08d}"}}}


def _paged_handler(total: int, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        start = int(params.get("pageToken", 0))
        size = int(params["pageSize"])
        body = {"studies": [_study(i) for i in range(start, min(start + size, total))]}
        if "countTotal" in params:
            body["totalCount"] = total
        if start + size < total:
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)

    return handler


def test_follows_page_tokens_and_repeats_params():
    seen: list = []
    result = _client(_paged_handler(2500, seen)).fetch_studies({"query.cond": "x"}, "NCTId", max_records=10_000)
    assert (len(result.studies), result.total, result.pages, result.truncated) == (2500, 2500, 3, False)
    assert [p.get("pageToken") for p in seen] == [None, "1000", "2000"]
    assert all(p["query.cond"] == "x" and p["fields"] == "NCTId" for p in seen)
    assert "countTotal" in seen[0] and all("countTotal" not in p for p in seen[1:])


def test_max_records_truncates():
    seen: list = []
    result = _client(_paged_handler(2500, seen)).fetch_studies({}, "NCTId", max_records=1200)
    assert (len(result.studies), result.total, result.truncated) == (1200, 2500, True)
    assert [p["pageSize"] for p in seen] == ["1000", "200"]


def test_retries_429_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"totalCount": 7, "studies": []})

    total, _ = _client(handler).count({})
    assert total == 7 and len(calls) == 3


def test_gives_up_after_max_retries():
    with pytest.raises(UpstreamError, match="unavailable"):
        _client(lambda r: httpx.Response(503), max_retries=2).count({})


def test_client_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text="bad filter")

    with pytest.raises(UpstreamError, match="HTTP 400"):
        _client(handler).count({})
    assert len(calls) == 1


def test_cache_avoids_second_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"totalCount": 3, "studies": []})

    client = _client(handler, tmp_path)
    first = client.count({"query.cond": "x"})
    second = client.count({"query.cond": "x"})
    assert first == second and len(calls) == 1
