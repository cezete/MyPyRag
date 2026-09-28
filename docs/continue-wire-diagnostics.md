# Continue–Ollama E05 wire diagnostics

This diagnostic mode is opt-in and does not change normal Continue, Ollama, or
MyPyRag behavior. It runs a loopback HTTP proxy between Continue and the real
Ollama endpoint. Full bodies are captured only for `/api/chat` requests whose
user message exactly matches `docs/continue-eval-v1/e05-prompt.txt`.

Generated data is written below `test-output/continue-wire-diag/`, which is
already excluded by `.gitignore` through the `test-output/` rule. Do not move
raw captures into a tracked directory.

## Start the capture

From the repository root on the machine running VS Code:

```powershell
.\.venv\Scripts\python.exe tools\continue_wire_diag.py start `
  --upstream http://192.168.3.32:11434 `
  --expected-prompt-file docs\continue-eval-v1\e05-prompt.txt `
  --run-id E05-01
```

The command prints the run directory and the temporary `apiBase`, normally
`http://127.0.0.1:11435`. It remains in the foreground and is stopped with
Ctrl+C. Nothing is captured while this process is not running.

Create a temporary debug-only model entry in the local Continue
`~/.continue/config.yaml`; keep the production entry unchanged:

```yaml
  - name: Mac Qwen3 Coder 30B wire debug
    provider: ollama
    model: qwen3-coder:30b
    apiBase: http://127.0.0.1:11435
    roles:
      - chat
      - edit
      - apply
    capabilities:
      - tool_use
    defaultCompletionOptions:
      contextLength: 16384
      maxTokens: 4096
```

Select this model only for the diagnostic session. The underlying provider,
model, context length, and output limit remain the same; only the network path
passes through the local recorder.

The proxy forwards nonmatching traffic but records metadata only. This guard
prevents an unrelated Continue chat from being copied into the E05 raw logs.
Authorization, token, API-key, secret, and password fields are redacted from
JSON artifacts. The proxy never prints request bodies or headers to stdout.

Do not use Continue's `VERBOSE_FETCH` environment switch for this experiment:
Continue 2.0.0 prints full headers and request bodies without redaction, and it
does not capture the response body.

## Attach the Continue session

After the new Continue chat has finished, obtain its UUID from the matching
file in `~/.continue/sessions/`, then run:

```powershell
.\.venv\Scripts\python.exe tools\continue_wire_diag.py collect `
  --run-dir test-output\continue-wire-diag\E05-01 `
  --session-id <CONTINUE_SESSION_UUID>
```

The collector writes a redacted copy of that session JSON, extracts only matching
`chatInteraction.jsonl` records, and writes `summary.json` plus `summary.md`.
It does not infer whether a post-tool final answer is technically correct.

## Artifacts

Each run contains:

- `manifest.json`: run ID, endpoint, timestamp, and expected-prompt hash;
- `requests/<request-id>/request.json`: redacted Ollama request;
- `requests/<request-id>/response.raw.ndjson`: exact captured response bytes;
- `requests/<request-id>/response.events.jsonl`: parsed, redacted events;
- `requests/<request-id>/summary.json`: request-level facts;
- `continue-session.json`: the selected Continue session;
- `continue-chat-interactions.jsonl`: matching assembled completions;
- `summary.json` and `summary.md`: correlated run summary.

Ollama tool calls do not carry the `tc_*` IDs generated later by Continue, so
provider and Continue calls are correlated by run, request order, tool name,
and arguments. The generated Continue IDs are retained in the session summary.

The summary can prove categories A and B directly and can identify an executor
failure consistent with C. If a tool result returned, deciding between D and a
fully successful run requires manual review of the final answer. The supplied
A–E taxonomy has no category for a fully successful run; record that case as
`PASS – no failure category` rather than mislabelling it D.

## Controlled five-run protocol

Perform the repetitions as separate steps named `E05-W01` through `E05-W05`.
Do not start the next repetition until the previous run has been collected and
reviewed.

For every repetition:

1. Confirm that the previous proxy is stopped and that the new run directory
   does not exist. Start a fresh proxy with the run ID for this repetition.
2. Confirm the manifest has the expected Git HEAD/status hash and prompt hash.
3. In Continue, select `Mac Qwen3 Coder 30B wire debug`, Agent mode, and start a
   new empty session. Do not reuse or continue an earlier chat.
4. Paste the contents of `e05-prompt.txt` without any change and submit it once.
5. Stop the run manually if it reaches three native tool calls or two minutes.
   Do not add follow-up text to the Continue session.
6. Stop the proxy, identify the new Continue session UUID, and run `collect`.
7. Check `summary.md`, the request-level raw events, and the Continue session
   before proceeding to the next repetition.

Keep the rule, MCP policy, repository state, model/provider, context length,
max output tokens, and Ollama model defaults unchanged across all five runs.
Do not rebuild the index, rename tools, edit the rule, change model, or retry a
failed tool manually. The proxy request artifact is the authoritative record
of the actual messages, tool schemas, and generation options sent to Ollama.

The completed five-run investigation and its evidence-indexed conclusions are
documented in `docs/continue-e05-wire-results.md`.
