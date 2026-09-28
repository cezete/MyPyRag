from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

MODULE_PATH = Path(__file__).parents[1] / "tools" / "continue_wire_diag.py"
SPEC = importlib.util.spec_from_file_location("continue_wire_diag", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
wire_diag = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wire_diag
SPEC.loader.exec_module(wire_diag)


def test_redact_recurses_without_changing_safe_values() -> None:
    value = {
        "Authorization": "Bearer secret",
        "messages": [{"content": "safe benchmark text"}],
        "nested": {"api_key": "secret", "temperature": 0.7},
    }

    assert wire_diag.redact(value) == {
        "Authorization": "<REDACTED>",
        "messages": [{"content": "safe benchmark text"}],
        "nested": {"api_key": "<REDACTED>", "temperature": 0.7},
    }


def test_capture_requires_exact_user_prompt() -> None:
    expected = "Exact E05 prompt"
    assert wire_diag.contains_exact_user_prompt(
        {"messages": [{"role": "user", "content": expected}]}, expected
    )
    assert not wire_diag.contains_exact_user_prompt(
        {"messages": [{"role": "user", "content": expected + " changed"}]}, expected
    )
    assert not wire_diag.contains_exact_user_prompt(
        {"messages": [{"role": "system", "content": expected}]}, expected
    )


def test_git_snapshot_has_stable_shape(tmp_path: Path) -> None:
    assert wire_diag.git_snapshot(tmp_path) == {
        "head": None,
        "branch": None,
        "status_sha256": None,
        "status_lines": 0,
    }


def test_provider_event_facts_distinguish_native_and_pseudo_calls() -> None:
    native = [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "mypyrag_nagypapi_search_docs", "arguments": {}}}
                ],
            },
            "done": True,
            "done_reason": "stop",
        }
    ]
    pseudo = [{"message": {"role": "assistant", "content": "<function=search_docs>"}}]

    assert wire_diag.event_facts(native)["provider_tool_call_count"] == 1
    assert not wire_diag.event_facts(native)["provider_pseudo_call"]
    assert wire_diag.event_facts(pseudo)["provider_tool_call_count"] == 0
    assert wire_diag.event_facts(pseudo)["provider_pseudo_call"]


def test_collect_run_writes_correlated_summary(tmp_path: Path) -> None:
    run_dir = tmp_path / "E05-test"
    request_dir = run_dir / "requests" / "req-001-deadbeef"
    request_dir.mkdir(parents=True)
    (request_dir / "summary.json").write_text(
        json.dumps(
            {
                "request_id": "req-001-deadbeef",
                "capture_enabled": True,
                "response_facts": {
                    "provider_tool_call_count": 0,
                    "provider_pseudo_call": True,
                },
            }
        ),
        encoding="utf-8",
    )
    continue_home = tmp_path / ".continue"
    (continue_home / "sessions").mkdir(parents=True)
    (continue_home / "dev_data" / "0.2.0").mkdir(parents=True)
    session_id = "00000000-0000-0000-0000-000000000001"
    session = {
        "sessionId": session_id,
        "mode": "agent",
        "chatModelTitle": "Mac Qwen3 Coder 30B wire debug",
        "history": [{"message": {"role": "assistant", "content": "<function=search_docs>"}}],
    }
    (continue_home / "sessions" / f"{session_id}.json").write_text(
        json.dumps(session), encoding="utf-8"
    )
    interaction = {"sessionId": session_id, "Authorization": "secret"}
    (continue_home / "dev_data" / "0.2.0" / "chatInteraction.jsonl").write_text(
        json.dumps(interaction) + "\n", encoding="utf-8"
    )

    wire_diag.collect_run(run_dir, session_id, continue_home)

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["classification_hint"] == "A"
    assert summary["wire"]["request_ids"] == ["req-001-deadbeef"]
    interactions = (run_dir / "continue-chat-interactions.jsonl").read_text(encoding="utf-8")
    assert "secret" not in interactions
    assert "<REDACTED>" in interactions


def test_proxy_preserves_stream_and_captures_exact_e05_request(tmp_path: Path) -> None:
    expected_prompt = "Exact E05 prompt"
    response_bytes = (
        b'{"message":{"role":"assistant","content":""},"done":false}\n'
        b'{"message":{"role":"assistant","content":"<function=search>"},"done":true}\n'
    )

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(response_bytes)))
            self.end_headers()
            self.wfile.write(response_bytes)

        def log_message(self, format: str, *args: object) -> None:
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    run_dir = tmp_path / "E05-proxy"
    (run_dir / "requests").mkdir(parents=True)
    context = wire_diag.CaptureContext(
        "E05-proxy",
        run_dir,
        f"http://127.0.0.1:{upstream.server_port}/",
        expected_prompt,
    )
    recorder = wire_diag.Recorder(context)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), wire_diag.make_handler(recorder))
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{proxy.server_port}/api/chat",
            json={
                "model": "qwen3-coder:30b",
                "messages": [{"role": "user", "content": expected_prompt}],
                "Authorization": "do-not-log",
            },
            timeout=5,
        )
        assert response.content == response_bytes
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()

    request_dirs = list((run_dir / "requests").iterdir())
    assert len(request_dirs) == 1
    captured_request = json.loads((request_dirs[0] / "request.json").read_text(encoding="utf-8"))
    assert captured_request["Authorization"] == "<REDACTED>"
    request_summary = json.loads((request_dirs[0] / "summary.json").read_text(encoding="utf-8"))
    assert request_summary["response_facts"]["provider_pseudo_call"] is True
