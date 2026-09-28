# E05 Continue–Ollama wire diagnostic results

Investigation dates: 2026-09-27 and 2026-09-28.

## Outcome

Five fresh Continue Agent sessions used the same E05 prompt, rule, MCP toolset,
repository state, Ollama provider, and `qwen3-coder:30b` model. The provider
wire output varied across repetitions:

- two runs returned XML-like pseudo-call text with no native `tool_calls`;
- two runs returned native tool calls whose required arguments were empty;
- one run returned a correct native call with valid arguments, received the
  decisive local source, and answered correctly.

This proves that the original pseudo-call symptom is already present in the
raw Ollama response for the affected runs. Continue did not discard a valid
structured call in those cases. It also proves a second, distinct provider
output failure: native calls can arrive with `{}` instead of the arguments
described by the request's tool schema.

## Fixed setup

- Prompt SHA-256:
  `1423d4bd6441b2d1637adadcd86979435d00a11adc3340579001c4633f910f09`
- Continue: 2.0.0, Agent mode, new empty session per run
- Debug model title: `Mac Qwen3 Coder 30B wire debug`
- Provider/model: Ollama 0.34.0 / `qwen3-coder:30b`
- Model digest:
  `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`
- Context/max output: 16384 / 4096
- Repository HEAD at capture start:
  `02846dc936797d22b945d87e3afbe7f778a0a987`
- Capture path: loopback proxy at `http://127.0.0.1:11435`, forwarding to the
  unchanged Ollama endpoint

The prompt is stored in `docs/continue-eval-v1/e05-prompt.txt`. Raw artifacts
remain ignored under `test-output/continue-wire-diag/`.

## Per-run classification

The supplied A–E taxonomy has no successful-run category. Therefore W03 is
recorded as `PASS – no failure category`; assigning it to D or E would be
factually incorrect.

| Run | Classification | Session | Provider evidence | Continue/result evidence |
| --- | --- | --- | --- | --- |
| E05-W01 | **C** | `5f66b146-c325-45ef-b76a-13bb34c579d4` | Four native calls in `req-004-f5e5f7cb` through `req-008-cf799905`; no pseudo-call | Continue created all four calls. `get_rag_status` completed; two `search_docs` and one `web_search` calls used `{}` and failed required-field validation. The final answer was unsupported and incorrect. |
| E05-W02 | **A** | `2192b7cd-f116-4198-8862-5762c6a5a96f` | `req-002-dce5d964`: `provider_tool_call_count=0`, `provider_pseudo_call=true`; content contains `<function=mypyrag_nagypapi_search_docs>` | Zero Continue calls, states, and tool messages; no substantive final answer. |
| E05-W03 | **PASS – no failure category** | `5bef5ce4-0670-47da-b072-9f5ce074bffe` | `req-002-56d3dd14`: one native `search_docs` call with query `C64 KERNAL PLOT routine X Y coordinates carry flag usage` and universe `retro.c64` | Continue executed it successfully. The response cited `C64_Programmers_Reference_Guide.pdf`, page 309, section `8-1 9. Function Name: PLOT`, and correctly selected A. |
| E05-W04 | **A** | `a65ea7e0-3326-4369-82fd-906cad273c9d` | `req-003-d0831c7f`: `provider_tool_call_count=0`, `provider_pseudo_call=true`; XML-like `search_docs` text is in the raw stream | Zero Continue calls, states, and tool messages; the displayed response stopped at the pseudo-call. |
| E05-W05 | **C** | `44017091-f600-4af4-b733-8ffef125e03c` | Four native calls in `req-002-8a929bfc` through `req-005-50c5608a`; no pseudo-call | Continue created all four calls. `get_rag_status` completed; two `search_docs` and one `web_search` calls used `{}` and failed required-field validation. The final answer first asserted the incorrect B convention, then called the evidence insufficient. |

W01 and W05 reached four native calls before the UI could be stopped after the
three-call threshold was observed. Both loops completed in seconds. This is a
documented harness-limit deviation, not an additional retry or follow-up.

## Evidence locations

Each ignored run directory contains the redacted request, exact raw NDJSON
response, parsed events, request summary, redacted Continue session, matching
Continue interaction records, and run summary. The primary entry points are:

- `test-output/continue-wire-diag/E05-W01/summary.md`
- `test-output/continue-wire-diag/E05-W02/summary.md`
- `test-output/continue-wire-diag/E05-W03/summary.md`
- `test-output/continue-wire-diag/E05-W04/summary.md`
- `test-output/continue-wire-diag/E05-W05/summary.md`

For an A classification, inspect the referenced request's
`response.raw.ndjson`, `response.events.jsonl`, and `summary.json`. For C and
PASS, correlate the request artifacts with `continue-session.json` and
`continue-chat-interactions.jsonl` in the same run directory.

## Conclusions by evidence strength

### Proven

1. W02 and W04 contain pseudo-call text in the raw provider stream and contain
   no native provider `tool_calls`. Continue cannot execute a structured call
   that it never received.
2. Whenever Ollama returned a native call in these five runs, Continue created
   a corresponding call and a tool-result message. No B case was observed.
3. W01 and W05 failed at argument generation/execution: the request advertised
   the required schemas, but the provider emitted `{}` for `search_docs` and
   `web_search`.
4. W03 demonstrates that the unchanged path can work end to end, including
   correct arguments, retrieval, feedback, source naming, and final reasoning.
5. The same fixed setup therefore produced A, C, and successful outcomes. The
   failure is intermittent and occurs before MCP/RAG in A, and at provider
   argument generation followed by executor validation in C.

### Supported interpretation

The strongest explanation is unstable tool-protocol adherence by this local
model/provider path. The Continue Ollama adapter maps native Ollama
`message.tool_calls` to Continue calls, but does not reinterpret XML-like text
as a tool call. That behavior is consistent with every captured run. The five
samples demonstrate the symptom and boundary; they are not a statistical
reliability estimate.

### Not observable or not established

- The captures do not contain token logits, sampling RNG state, or an explicit
  seed, so they cannot explain why identical visible settings diverged.
- They do not establish whether Qwen's model code or Ollama's tool-use
  templating is the deeper origin of the malformed representation/arguments.
- No valid provider call was lost by Continue in this sample, so category B is
  neither observed nor supported here.
- No run had successful relevant evidence followed by an incorrect decision;
  category D was not observed in this five-run series.
- The diagnostic deliberately did not change the rule, tool names, RAG index,
  production behavior, or model.

## Reproduction

Start the proxy from the repository root:

```powershell
.\.venv\Scripts\python.exe tools\continue_wire_diag.py start `
  --upstream http://192.168.3.32:11434 `
  --expected-prompt-file docs\continue-eval-v1\e05-prompt.txt `
  --run-id E05-W06
```

In VS Code, select Agent mode and `Mac Qwen3 Coder 30B wire debug`, start a new
empty Continue session, paste the exact contents of `e05-prompt.txt`, and submit
once. Stop at three native calls or two minutes. Then attach the new session:

```powershell
.\.venv\Scripts\python.exe tools\continue_wire_diag.py collect `
  --run-dir test-output\continue-wire-diag\E05-W06 `
  --session-id <CONTINUE_SESSION_UUID>
```
