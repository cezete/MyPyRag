"""Opt-in, E05-scoped wire capture for Continue's Ollama provider.

The proxy forwards Ollama HTTP traffic unchanged while capturing full request
bodies and raw response bytes only when an /api/chat request contains the
expected E05 prompt as an exact user message. Other requests receive metadata-
only records. Authorization-like fields are always redacted in JSON artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

DEFAULT_OUTPUT_ROOT = Path("test-output/continue-wire-diag")
SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|secret|password|cookie)",
    re.IGNORECASE,
)
PSEUDO_CALL = re.compile(
    r"<function=|<tool_call\b|<function_calls?\b|<invoke\b",
    re.IGNORECASE,
)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def redact(value: Any, parent_key: str = "") -> Any:
    if SENSITIVE_KEY.search(parent_key):
        return "<REDACTED>"
    if isinstance(value, dict):
        return {str(key): redact(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: "<REDACTED>" if SENSITIVE_KEY.search(key) else value for key, value in headers.items()
    }


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") in {None, "text"}
        )
    return ""


def contains_exact_user_prompt(body: Any, expected_prompt: str) -> bool:
    if not isinstance(body, dict):
        return False
    for message in body.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if message_text(message.get("content")).strip() == expected_prompt.strip():
            return True
    return False


def safe_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise ValueError("run ID must use 1-80 letters, digits, dots, underscores, or dashes")
    return value


def default_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"E05-{stamp}-{uuid.uuid4().hex[:8]}"


def git_snapshot(directory: Path) -> dict[str, Any]:
    def run_git(*arguments: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return result.stdout.strip()

    head = run_git("rev-parse", "HEAD")
    branch = run_git("branch", "--show-current")
    status = run_git("status", "--porcelain=v2", "--untracked-files=all")
    return {
        "head": head,
        "branch": branch,
        "status_sha256": hashlib.sha256((status or "").encode()).hexdigest()
        if status is not None
        else None,
        "status_lines": len(status.splitlines()) if status else 0,
    }


def parse_ndjson(raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"line {line_number}: {exc}")
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            errors.append(f"line {line_number}: JSON value is not an object")
    return events, errors


def event_facts(events: list[dict[str, Any]]) -> dict[str, Any]:
    tool_calls: list[dict[str, Any]] = []
    content_parts: list[str] = []
    done_reasons: list[str] = []
    provider_errors: list[str] = []
    for event in events:
        if event.get("error"):
            provider_errors.append(str(event["error"]))
        if event.get("done_reason"):
            done_reasons.append(str(event["done_reason"]))
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            content_parts.append(content)
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                tool_calls.append(call)
    content = "".join(content_parts)
    return {
        "event_count": len(events),
        "provider_tool_call_count": len(tool_calls),
        "provider_tool_names": [((call.get("function") or {}).get("name")) for call in tool_calls],
        "provider_content_chars": len(content),
        "provider_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "provider_pseudo_call": bool(PSEUDO_CALL.search(content)),
        "done_reasons": done_reasons,
        "provider_errors": provider_errors,
    }


@dataclass(frozen=True)
class CaptureContext:
    run_id: str
    run_dir: Path
    upstream: str
    expected_prompt: str


class Recorder:
    def __init__(self, context: CaptureContext) -> None:
        self.context = context
        self._lock = threading.Lock()
        self._sequence = 0

    def next_request(self) -> tuple[int, str, Path]:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        request_id = f"req-{sequence:03d}-{uuid.uuid4().hex[:8]}"
        request_dir = self.context.run_dir / "requests" / request_id
        request_dir.mkdir(parents=True, exist_ok=False)
        return sequence, request_id, request_dir

    def refresh_summary(self) -> None:
        summaries: list[dict[str, Any]] = []
        for path in sorted((self.context.run_dir / "requests").glob("*/summary.json")):
            summaries.append(json.loads(path.read_text(encoding="utf-8")))
        json_write(
            self.context.run_dir / "wire-summary.json",
            {"run_id": self.context.run_id, "requests": summaries},
        )
        captured = [item for item in summaries if item.get("capture_enabled")]
        lines = [
            f"# Continue/Ollama wire summary – {self.context.run_id}",
            "",
            f"- Requests observed: {len(summaries)}",
            f"- E05 chat requests captured: {len(captured)}",
        ]
        for item in captured:
            facts = item.get("response_facts", {})
            lines.extend(
                [
                    "",
                    f"## {item['request_id']}",
                    "",
                    f"- HTTP status: {item.get('response_status')}",
                    f"- Raw events: {facts.get('event_count', 0)}",
                    f"- Provider tool calls: {facts.get('provider_tool_call_count', 0)}",
                    f"- Provider pseudo-call text: {facts.get('provider_pseudo_call', False)}",
                ]
            )
        (self.context.run_dir / "wire-summary.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


def make_handler(recorder: Recorder) -> type[BaseHTTPRequestHandler]:
    class ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._proxy()

        def do_POST(self) -> None:
            self._proxy()

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"[{utc_now()}] {format % args}\n")

        def _proxy(self) -> None:
            sequence, request_id, request_dir = recorder.next_request()
            started_at = utc_now()
            length = int(self.headers.get("Content-Length", "0"))
            body_bytes = self.rfile.read(length) if length else b""
            parsed_body: Any = None
            if body_bytes:
                try:
                    parsed_body = json.loads(body_bytes)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed_body = None
            endpoint_path = urlsplit(self.path).path
            capture = endpoint_path == "/api/chat" and contains_exact_user_prompt(
                parsed_body, recorder.context.expected_prompt
            )
            request_meta = {
                "sequence": sequence,
                "request_id": request_id,
                "started_at": started_at,
                "method": self.command,
                "path": endpoint_path,
                "headers": redact_headers(dict(self.headers.items())),
                "body_bytes": len(body_bytes),
                "capture_enabled": capture,
                "capture_skip_reason": None if capture else "not an exact E05 /api/chat request",
            }
            json_write(request_dir / "request-meta.json", request_meta)
            if capture:
                json_write(request_dir / "request.json", redact(parsed_body))

            upstream_url = urljoin(
                recorder.context.upstream.rstrip("/") + "/", self.path.lstrip("/")
            )
            outbound_headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower()
                not in HOP_BY_HOP_HEADERS | {"host", "content-length", "accept-encoding"}
            }
            outbound_headers["Accept-Encoding"] = "identity"
            raw_response = bytearray()
            response_status: int | None = None
            response_headers: dict[str, str] = {}
            proxy_error: str | None = None
            try:
                timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
                with (
                    httpx.Client(timeout=timeout) as client,
                    client.stream(
                        self.command,
                        upstream_url,
                        headers=outbound_headers,
                        content=body_bytes,
                    ) as response,
                ):
                    response_status = response.status_code
                    response_headers = dict(response.headers)
                    self.send_response(response.status_code)
                    for key, value in response.headers.items():
                        lowered = key.lower()
                        if lowered in HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}:
                            continue
                        self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    for chunk in response.iter_raw():
                        if capture:
                            raw_response.extend(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - proxy boundary
                proxy_error = f"{type(exc).__name__}: {exc}"
                if response_status is None and not self.wfile.closed:
                    try:
                        self.send_error(502, "Ollama diagnostic proxy upstream error")
                    except OSError:
                        pass

            facts: dict[str, Any] = {}
            parse_errors: list[str] = []
            if capture:
                (request_dir / "response.raw.ndjson").write_bytes(raw_response)
                events, parse_errors = parse_ndjson(bytes(raw_response))
                with (request_dir / "response.events.jsonl").open("w", encoding="utf-8") as handle:
                    for event in events:
                        handle.write(json.dumps(redact(event), ensure_ascii=False) + "\n")
                facts = event_facts(events)
            json_write(
                request_dir / "summary.json",
                {
                    "sequence": sequence,
                    "request_id": request_id,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "path": endpoint_path,
                    "capture_enabled": capture,
                    "response_status": response_status,
                    "response_headers": redact_headers(response_headers),
                    "response_bytes": len(raw_response) if capture else None,
                    "response_parse_errors": parse_errors,
                    "response_facts": facts,
                    "proxy_error": proxy_error,
                },
            )
            recorder.refresh_summary()

    return ProxyHandler


def session_facts(session: dict[str, Any]) -> dict[str, Any]:
    native_calls: list[dict[str, Any]] = []
    state_statuses: list[str] = []
    tool_roles = 0
    pseudo_messages = 0
    for item in session.get("history", []):
        if not isinstance(item, dict):
            continue
        message = item.get("message") or {}
        if message.get("role") == "tool":
            tool_roles += 1
        calls = message.get("toolCalls") or []
        native_calls.extend(call for call in calls if isinstance(call, dict))
        if (
            message.get("role") == "assistant"
            and not calls
            and PSEUDO_CALL.search(message_text(message.get("content")))
        ):
            pseudo_messages += 1
        for state in item.get("toolCallStates") or []:
            if isinstance(state, dict) and state.get("status"):
                state_statuses.append(str(state["status"]))
    return {
        "session_id": session.get("sessionId"),
        "mode": session.get("mode"),
        "chat_model_title": session.get("chatModelTitle"),
        "history_count": len(session.get("history", [])),
        "continue_tool_call_count": len(native_calls),
        "continue_tool_call_ids": [call.get("id") for call in native_calls],
        "continue_tool_names": [
            ((call.get("function") or {}).get("name")) for call in native_calls
        ],
        "tool_call_state_statuses": state_statuses,
        "tool_role_message_count": tool_roles,
        "assistant_pseudo_call_messages": pseudo_messages,
    }


def classification_hint(wire: dict[str, Any], session: dict[str, Any]) -> tuple[str, str]:
    provider_calls = int(wire.get("provider_tool_call_count", 0))
    provider_pseudo = bool(wire.get("provider_pseudo_call"))
    continue_calls = int(session.get("continue_tool_call_count", 0))
    tool_roles = int(session.get("tool_role_message_count", 0))
    statuses = session.get("tool_call_state_statuses", [])
    if provider_pseudo and provider_calls == 0:
        return (
            "A",
            "The captured Ollama stream already contains pseudo-call text and no tool_calls.",
        )
    if provider_calls > 0 and continue_calls == 0:
        return "B", "Ollama emitted tool_calls, but the Continue session contains none."
    if continue_calls > 0 and any(status == "errored" for status in statuses) and tool_roles == 0:
        return (
            "C",
            "Continue recognized calls, but no tool response was recorded and a call errored.",
        )
    if tool_roles > 0:
        return (
            "MANUAL",
            "Tool results returned; inspect final-answer correctness to distinguish D from a successful run.",
        )
    return "E", "The available artifacts do not expose enough evidence for A-D."


def collect_run(run_dir: Path, session_id: str, continue_home: Path) -> None:
    session_source = continue_home / "sessions" / f"{session_id}.json"
    if not session_source.is_file():
        raise FileNotFoundError(f"Continue session not found: {session_source}")
    session = json.loads(session_source.read_text(encoding="utf-8"))
    if session.get("sessionId") != session_id:
        raise ValueError("sessionId inside the Continue session does not match")
    session_target = run_dir / "continue-session.json"
    json_write(session_target, redact(session))

    chat_records: list[dict[str, Any]] = []
    chat_path = continue_home / "dev_data" / "0.2.0" / "chatInteraction.jsonl"
    if chat_path.is_file():
        for line in chat_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("sessionId") == session_id:
                chat_records.append(redact(record))
    with (run_dir / "continue-chat-interactions.jsonl").open("w", encoding="utf-8") as handle:
        for record in chat_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    session_summary = session_facts(session)
    wire_summaries = []
    for path in sorted((run_dir / "requests").glob("*/summary.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("capture_enabled"):
            wire_summaries.append(item)
    aggregate_wire: dict[str, Any] = {
        "provider_tool_call_count": sum(
            int(item.get("response_facts", {}).get("provider_tool_call_count", 0))
            for item in wire_summaries
        ),
        "provider_pseudo_call": any(
            bool(item.get("response_facts", {}).get("provider_pseudo_call"))
            for item in wire_summaries
        ),
        "captured_chat_request_count": len(wire_summaries),
        "request_ids": [item.get("request_id") for item in wire_summaries],
    }
    category, reason = classification_hint(aggregate_wire, session_summary)
    combined = {
        "run_id": run_dir.name,
        "collected_at": utc_now(),
        "wire": aggregate_wire,
        "continue": session_summary,
        "chat_interaction_records": len(chat_records),
        "classification_hint": category,
        "classification_reason": reason,
    }
    json_write(run_dir / "summary.json", combined)
    lines = [
        f"# E05 diagnostic summary – {run_dir.name}",
        "",
        f"- Session: `{session_id}`",
        "- Wire request IDs: "
        + (", ".join(str(item) for item in aggregate_wire["request_ids"]) or "none"),
        f"- Captured E05 chat requests: {aggregate_wire['captured_chat_request_count']}",
        f"- Provider tool calls: {aggregate_wire['provider_tool_call_count']}",
        f"- Provider pseudo-call text: {aggregate_wire['provider_pseudo_call']}",
        f"- Continue tool calls: {session_summary['continue_tool_call_count']}",
        f"- Continue tool states: {', '.join(session_summary['tool_call_state_statuses']) or 'none'}",
        f"- Tool response messages: {session_summary['tool_role_message_count']}",
        f"- Classification hint: **{category}**",
        f"- Reason: {reason}",
        "",
        "The hint is evidence routing, not a correctness judgment. D versus a successful",
        "run always requires manual review of the final answer.",
    ]
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def start_proxy(args: argparse.Namespace) -> None:
    run_id = safe_run_id(args.run_id or default_run_id())
    run_dir = args.output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "requests").mkdir()
    expected_prompt = args.expected_prompt_file.read_text(encoding="utf-8").strip()
    if not expected_prompt:
        raise ValueError("expected prompt file is empty")
    upstream = args.upstream.rstrip("/") + "/"
    parsed_upstream = urlsplit(upstream)
    if parsed_upstream.scheme not in {"http", "https"} or not parsed_upstream.netloc:
        raise ValueError("upstream must be an absolute HTTP(S) URL")
    if parsed_upstream.username or parsed_upstream.password:
        raise ValueError("upstream URL must not contain credentials")
    if parsed_upstream.query or parsed_upstream.fragment:
        raise ValueError("upstream URL must not contain a query or fragment")
    context = CaptureContext(run_id, run_dir, upstream, expected_prompt)
    json_write(
        run_dir / "manifest.json",
        {
            "run_id": run_id,
            "started_at": utc_now(),
            "listen": f"http://{args.listen}:{args.port}",
            "upstream": upstream,
            "expected_prompt_sha256": hashlib.sha256(expected_prompt.encode()).hexdigest(),
            "capture_scope": "exact E05 user prompt on /api/chat",
            "git": git_snapshot(Path.cwd()),
        },
    )
    recorder = Recorder(context)
    recorder.refresh_summary()
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(recorder))
    print(f"run_id={run_id}")
    print(f"output={run_dir}")
    print(f"apiBase=http://{args.listen}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        recorder.refresh_summary()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="start the opt-in Ollama proxy")
    start.add_argument("--upstream", required=True, help="real Ollama base URL")
    start.add_argument("--expected-prompt-file", type=Path, required=True)
    start.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    start.add_argument("--run-id")
    start.add_argument("--listen", default="127.0.0.1")
    start.add_argument("--port", type=int, default=11435)
    start.set_defaults(func=start_proxy)

    collect = subparsers.add_parser("collect", help="attach Continue artifacts and summarize")
    collect.add_argument("--run-dir", type=Path, required=True)
    collect.add_argument("--session-id", required=True)
    collect.add_argument("--continue-home", type=Path, default=Path.home() / ".continue")
    collect.set_defaults(
        func=lambda args: collect_run(args.run_dir.resolve(), args.session_id, args.continue_home)
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
