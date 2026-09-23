import asyncio
from dataclasses import replace

import httpx
import pytest

from mypyrag.config import Config
from mypyrag.web_search import SearxngSearchClient, WebSearchError


def configured(tmp_path, monkeypatch, **overrides):
    for key in list(__import__("os").environ):
        if key.startswith("MYPYRAG_"):
            monkeypatch.delenv(key)
    selected = {"web_search_enabled": True, **overrides}
    return replace(Config.load(tmp_path), **selected)


def run(coro):
    return asyncio.run(coro)


def client_for(config, handler):
    http = httpx.AsyncClient(
        base_url="http://searxng", transport=httpx.MockTransport(handler)
    )
    return SearxngSearchClient(config, client=http), http


def error_from(coro):
    with pytest.raises(WebSearchError) as caught:
        run(coro)
    return caught.value


def test_maps_ranking_limits_deduplicates_and_normalizes(tmp_path, monkeypatch):
    config = configured(tmp_path, monkeypatch, web_search_max_snippet_chars=12)

    def handler(request):
        assert request.url.path == "/search"
        assert request.url.params["q"] == "C64 PLOT X & Y"
        assert request.url.params["format"] == "json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": " <b>First</b> ",
                        "url": "HTTPS://Example.com/doc#part",
                        "content": "alpha&nbsp;  beta and extra",
                    },
                    {
                        "title": "duplicate",
                        "url": "https://example.com/doc",
                        "content": "ignored",
                    },
                    {
                        "title": "bad",
                        "url": "javascript:alert(1)",
                        "content": "ignored",
                    },
                    {
                        "title": "Second",
                        "url": "http://example.org/two?q=1",
                        "content": "plain",
                    },
                    {
                        "title": "Third",
                        "url": "https://example.net/three",
                        "content": "not returned",
                    },
                ],
                "unresponsive_engines": [["google", "rate limited"]],
            },
        )

    backend, http = client_for(config, handler)
    result = run(backend.search("  C64 PLOT X & Y  ", 2))
    assert result["status"] == "ok"
    assert result["query"] == "C64 PLOT X & Y"
    assert result["provider"] == "searxng"
    assert result["result_count"] == 2
    assert result["results"] == [
        {
            "rank": 1,
            "title": "First",
            "url": "https://example.com/doc",
            "snippet": "alpha beta a",
        },
        {
            "rank": 2,
            "title": "Second",
            "url": "http://example.org/two?q=1",
            "snippet": "plain",
        },
    ]
    assert result["warnings"] == ["google: rate limited"]
    assert isinstance(result["timings_ms"]["total"], float)
    run(http.aclose())


@pytest.mark.parametrize("query", ["", "   "])
def test_rejects_empty_query(tmp_path, monkeypatch, query):
    config = configured(tmp_path, monkeypatch)
    backend, http = client_for(config, lambda _request: httpx.Response(500))
    error = error_from(backend.search(query))
    assert error.as_dict() == {
        "code": "invalid_input",
        "message": "query must not be empty",
        "retryable": False,
    }
    run(http.aclose())


def test_rejects_long_query_and_result_count_bounds(tmp_path, monkeypatch):
    config = configured(tmp_path, monkeypatch, web_search_max_query_chars=3)
    backend, http = client_for(config, lambda _request: httpx.Response(500))
    assert error_from(backend.search("long")).code == "invalid_input"
    for value in (0, 11, True):
        assert error_from(backend.search("ok", value)).code == "invalid_input"
    run(http.aclose())


def test_empty_results_are_distinct_from_total_upstream_failure(tmp_path, monkeypatch):
    config = configured(tmp_path, monkeypatch)
    responses = iter(
        [
            httpx.Response(200, json={"results": [], "unresponsive_engines": []}),
            httpx.Response(
                200,
                json={"results": [], "unresponsive_engines": [["bing", "timeout"]]},
            ),
        ]
    )
    backend, http = client_for(config, lambda _request: next(responses))
    assert run(backend.search("nothing"))["status"] == "no_results"
    error = error_from(backend.search("failure"))
    assert error.code == "upstream_unavailable"
    assert error.retryable is True
    run(http.aclose())


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
def test_http_errors_are_safe_and_classified(tmp_path, monkeypatch, status):
    config = configured(tmp_path, monkeypatch)
    attempts = 0

    def handler(_request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, content=b"<html>secret diagnostics</html>")

    backend, http = client_for(config, handler)
    error = error_from(backend.search("query"))
    assert error.code == "provider_http_error"
    assert str(status) in error.message
    assert "secret" not in error.message
    assert error.retryable is (status == 429 or status >= 500)
    assert attempts == (2 if error.retryable else 1)
    run(http.aclose())


def test_timeout_and_connection_failures_retry_once(tmp_path, monkeypatch):
    config = configured(tmp_path, monkeypatch)
    for exception, code in [
        (httpx.ReadTimeout("slow"), "provider_timeout"),
        (httpx.ConnectError("offline"), "provider_connection_error"),
    ]:
        attempts = 0

        def handler(_request, selected_exception=exception):
            nonlocal attempts
            attempts += 1
            raise selected_exception

        backend, http = client_for(config, handler)
        error = error_from(backend.search("query"))
        assert error.code == code
        assert error.retryable is True
        assert attempts == 2
        run(http.aclose())


def test_malformed_json_and_response_size_limit(tmp_path, monkeypatch):
    config = configured(tmp_path, monkeypatch, web_search_max_response_bytes=20)
    responses = iter(
        [
            httpx.Response(200, content=b"no"),
            httpx.Response(200, content=b'\x7b"results":[],' + b" " * 30 + b"}"),
        ]
    )
    backend, http = client_for(config, lambda _request: next(responses))
    assert error_from(backend.search("query")).code == "malformed_response"
    assert error_from(backend.search("query")).code == "response_too_large"
    run(http.aclose())


def test_disabled_and_not_configured(tmp_path, monkeypatch):
    disabled = configured(tmp_path, monkeypatch, web_search_enabled=False)
    backend, http = client_for(disabled, lambda _request: httpx.Response(500))
    assert error_from(backend.search("query")).code == "web_search_disabled"
    run(http.aclose())

    missing = configured(tmp_path, monkeypatch, web_search_backend_url="")
    backend, http = client_for(missing, lambda _request: httpx.Response(500))
    assert error_from(backend.search("query")).code == "web_search_not_configured"
    run(http.aclose())
