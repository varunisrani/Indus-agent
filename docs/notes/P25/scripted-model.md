# Task: Scripted model server for both apps (P25-06) Phase: 25 Owner: wave-41 builder Q1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — the row reads only the public
Chat Completions protocol (request body, `chat.completion.chunk` stream frames, `[DONE]` sentinel,
tool-call deltas). Process from the clean-room skill, reference 03 §5 (scripted local agents).
Evidence: none.

What it does (own words): a small HTTP server on loopback that speaks the OpenAI-compatible Chat
Completions endpoint, so both apps (ours and the reference, which both accept a custom base URL) can
hold the same scripted conversation during a parity run. The script is chosen by name — the URL
prefix `/<script>/v1` or the request's `model` field — and the server plays it deterministically.
Scripts decide the next answer from the conversation the client sends (stateless): a fresh user
turn starts the script, a trailing tool result moves it to its second half. So a retry or a second
app gets the same answer for the same history.

Scripts (names shared with the scenario table and the ledger):
- `echo` — streams `You said: <last user text>`.
- `approve_or_decline` — asks to run one shell command (`echo parity-approved`) through the run-a-
  command tool the client offered; when the tool result reads as refused (denied / declined /
  blocked / not approved) it answers that nothing ran, otherwise it reports the first output line.
- `clarify` — asks one question with two choices through the ask-the-user tool; then reads the
  answer back.
- `edit_file` — writes one file through the write-a-file tool; the path is a fixed relative name
  or, with `--edit-root DIR`, that name inside DIR (anything that would leave DIR is refused when
  the server starts).
- `long_stream` — a long reply in many small frames with a pause between frames (interrupt flows).
- `hang` — one short frame, then only keep-alive comments until the client goes away (or a cap).

Tools are found by shape, not by name: the run tool is the offered function whose schema requires
`command`; the ask tool takes `questions` (a list, our form) or `question`; the write tool requires
`path` and `content`. Ties go to the fewest required parameters, then the name. `--tool
role=name` pins one. A request that offers no matching tool (title or summary side requests) gets a
short plain answer, never an error.

Inputs → outputs: `python scripts/parity/scripted_model_server.py [--host 127.0.0.1] [--port 0]
[--default-script NAME] [--edit-root DIR] [--frame-delay-ms N] [--log PATH|-]` → first stdout line
`{"port": n}`; `POST …/chat/completions` (stream or not) → SSE frames or one JSON body;
`GET …/models` → the scripts as models; unknown script → 400 with an error body; with `--log` one
JSON line per request, frame and ending (`done` / `cancelled`). Without `--log` nothing is logged.

Files: `scripts/parity/scripted_model_server.py`, `scripts/parity/__init__.py`,
`tests/unit/parity/test_scripted_model_server.py`.

Edge cases / failure modes:
- client disconnect mid-stream (interrupt): the write fails, the play stops, `cancelled` is logged;
  `hang` notices through its keep-alive writes;
- malformed JSON body → 400; unknown path → 404; non-loopback `--host` → refused at start;
- a non-streaming request gets the same plan as one JSON body (tool calls included);
- `stream_options.include_usage` adds a usage frame before `[DONE]`;
- tool-call arguments are sent in fragments (clients must join them).

Our design differences / decisions:
- stdlib `http.server` (threaded, chunked transfer) — no new dependency; the scripts are code, not
  YAML, because `approve_or_decline` and `clarify` branch on the tool result.
- No real sleeps: pacing waits on a `threading.Event` (stop event) so shutdown is immediate.

Tests we will write (≥ 7): stream framing (role frame, content deltas, finish, `[DONE]`); tool call
emission (id/name first, fragments join to the JSON arguments); deny branch vs allow branch;
clarify ask + read-back; edit confined to the root (path inside, escape refused); cancel on client
disconnect (`long_stream` and `hang` log `cancelled`); unknown script → 400; models list; non-stream
body; tool picked by shape for a reference-like schema (`question` form); no log without `--log`.

## Decisions for the lead
- Script names mirror the scenario table, not `tests/fixtures/model_scripts/` (our fake model's
  YAML scripts cannot branch on tool results). Both apps use this server in parity runs, so the
  conversation is the same on both sides by construction. To mirror further, the e2e fake can import
  `SCRIPTS` from here.
- Model id = script name on both sides (`model.default: approve_or_decline`); the URL prefix form
  exists for clients that rewrite model ids.

Compare checklist (filled after the rewrite; reopened skill reference 03 §5 — no reference product
files apply):
- [x] mirrors the product fake's script names in the runner table (`tests/unit/parity/test_runner.py::test_every_flow_names_a_known_script_and_has_a_scenario`)
- [x] allow and decline answer differently (`tests/unit/parity/test_scripted_model_server.py::test_approve_or_decline_branches_on_the_tool_result`)
- [x] questions read back (`::test_clarify_asks_then_reads_the_answer_back`)
- [x] edits confined (`::test_edit_file_stays_inside_the_edit_root`)
- [x] cancel ends the waiting turn (`::test_client_disconnect_cancels_the_stream`)
- [x] empty answer for setup probes: `GET /models` lists scripts; side requests without tools get
  plain text (`::test_side_request_without_tools_gets_plain_text`)
- [x] frames logged only with `--log` (`::test_nothing_is_logged_without_a_log_sink`)
- Deliberate differences: OpenAI-compatible HTTP instead of an agent protocol (the seam both apps
  share here is a custom base URL).
- Not taken: a `--version`/probe surface (HTTP needs none).

## Status

Done (2026-10-21). See the runner note for how flows start it.
