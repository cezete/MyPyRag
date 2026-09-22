import asyncio
import socket
import threading
import time
from dataclasses import replace

import httpx
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError

from mypyrag.config import Config
from mypyrag.mcp_server import RagHttpClient, create_app


def configured(tmp_path, monkeypatch, **overrides):
    for key in list(__import__("os").environ):
        if key.startswith("MYPYRAG_"):
            monkeypatch.delenv(key)
    base = Config.load(tmp_path)
    return replace(
        base,
        api_token="backend-secret",
        mcp_access_token="mcp-secret",
        **overrides,
    )


def sample_response(*, rerank_applied=True, results=None):
    if results is None:
        results = [
            {
                "score": 0.75,
                "rerank_score": 2.5 if rerank_applied else None,
                "text": "PRINT displays text or numeric results.",
                "source_path": "docs/IN/retro/c64/manual.pdf",
                "chunk_index": 9,
                "metadata": {
                    "universe": "retro.c64",
                    "title": "PRINT",
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "page_numbers": [42],
                    "structural_path": ["BASIC", "PRINT"],
                },
            }
        ]
    return {
        "query": "What does PRINT do?",
        "metadata": {
            "rerank_applied": rerank_applied,
            "timings_ms": {"total": 12.5},
        },
        "results": results,
    }


def run(coro):
    return asyncio.run(coro)


def test_search_maps_query_and_universe_and_preserves_response(tmp_path, monkeypatch):
    config = configured(tmp_path, monkeypatch)
    expected = sample_response()

    async def handler(request):
        assert request.url.path == "/search"
        assert request.headers["authorization"] == "Bearer backend-secret"
        assert __import__("json").loads(request.content) == {
            "query": "What does PRINT do?",
            "universe": "retro.c64",
        }
        return httpx.Response(200, json=expected)

    client = httpx.AsyncClient(
        base_url="http://backend",
        headers={"Authorization": "Bearer backend-secret"},
        transport=httpx.MockTransport(handler),
    )
    backend = RagHttpClient(config, client=client)
    assert run(backend.search("What does PRINT do?", "retro.c64")) == expected
    run(client.aclose())


def test_search_preserves_empty_results_and_rerank_disabled(tmp_path, monkeypatch):
    config = configured(tmp_path, monkeypatch)
    expected = sample_response(rerank_applied=False, results=[])
    client = httpx.AsyncClient(
        base_url="http://backend",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=expected)),
    )
    backend = RagHttpClient(config, client=client)
    assert run(backend.search("q", "retro.c64")) == expected
    run(client.aclose())


def test_backend_errors_are_distinct(tmp_path, monkeypatch):
    config = configured(tmp_path, monkeypatch)

    async def exercise(response_or_error):
        def handler(request):
            if isinstance(response_or_error, Exception):
                raise response_or_error
            return response_or_error

        client = httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(handler))
        backend = RagHttpClient(config, client=client)
        try:
            await backend.search("q", "retro.c64")
        except ToolError as exc:
            return str(exc)
        finally:
            await client.aclose()
        raise AssertionError("Expected a tool error")

    messages = [
        run(exercise(httpx.ReadTimeout("slow"))),
        run(exercise(httpx.Response(401, json={"detail": "no"}))),
        run(exercise(httpx.Response(422, json={"detail": "bad universe"}))),
        run(exercise(httpx.Response(503, json={"detail": "down"}))),
        run(exercise(httpx.Response(200, content=b"not-json"))),
    ]
    assert "timed out" in messages[0]
    assert "credentials" in messages[1]
    assert "bad universe" in messages[2]
    assert "HTTP 503" in messages[3]
    assert "invalid JSON" in messages[4]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_streamable_http_protocol_auth_and_backend_recovery(tmp_path, monkeypatch):
    port = _free_port()
    config = configured(
        tmp_path,
        monkeypatch,
        mcp_port=port,
        mcp_allowed_hosts=f"127.0.0.1:{port}",
    )
    state = {"online": False}

    def backend_handler(request):
        if request.url.path == "/health":
            if not state["online"]:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json={"status": "ok", "database": "ok"})
        if not state["online"]:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=sample_response())

    backend_http = httpx.AsyncClient(
        base_url="http://backend",
        transport=httpx.MockTransport(backend_handler),
    )
    app = create_app(config, backend=RagHttpClient(config, client=backend_http))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    async def protocol_check():
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient() as raw:
            denied = await raw.post(url, json={})
            assert denied.status_code == 401
            denied = await raw.post(url, headers={"Authorization": "Bearer wrong"}, json={})
            assert denied.status_code == 401

        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer mcp-secret"}, timeout=30
        ) as authenticated:
            transport = streamable_http_client(url, http_client=authenticated)
            async with Client(transport, mode="legacy") as mcp_client:
                tools = await mcp_client.list_tools()
                assert [tool.name for tool in tools.tools] == ["search_docs", "get_rag_status"]
                assert tools.tools[0].input_schema["required"] == ["query", "universe"]

                down = await mcp_client.call_tool("get_rag_status")
                assert down.is_error is False
                assert down.structured_content["status"] == "unavailable"
                failed = await mcp_client.call_tool(
                    "search_docs", {"query": "question", "universe": "retro.c64"}
                )
                assert failed.is_error is True
                assert "unreachable" in failed.content[0].text

                state["online"] = True
                ready = await mcp_client.call_tool("get_rag_status")
                assert ready.structured_content["status"] == "ready"
                found = await mcp_client.call_tool(
                    "search_docs",
                    {"query": "What does PRINT do?", "universe": "retro.c64"},
                )
                assert found.is_error is False
                assert found.structured_content == sample_response()
                assert "manual.pdf" in found.content[0].text

                empty_query = await mcp_client.call_tool(
                    "search_docs", {"query": "   ", "universe": "retro.c64"}
                )
                assert empty_query.is_error is True

    try:
        run(protocol_check())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        run(backend_http.aclose())
    assert not thread.is_alive()
