# Task: Bridge session and prompt methods, event projection (P12-29) Phase: 12 Owner: wave-7 builder G

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/methods_session.py`
(`session.create`, `session.resume`, `session.list`, `session.most_recent` handlers and the row
summary / listing deny-list helpers only), `tui_gateway/methods_session_interrupt.py` (whole),
`tui_gateway/tool_progress.py` (verbose caps, start/complete payloads, todo projection),
`tui_gateway/session_history.py` (`_history_to_messages` head only, to see what the display
projection drops). Research `05-tui.md` §3.4–3.6, §3.13–3.16, R05.6, R05.21. Ours:
`docs/notes/P04/session-service.md`, `agent-object.md`, `interrupts.md`, `protocol-models.md`.
Evidence: none.

What it does (own words): the bridge is a thin mapping onto `SessionService`. Session methods
create, resume (by id or exact title), list, close and inspect sessions; prompt methods start turns
and steer the running one. Every service event of an attached session is forwarded as a
notification named by its type, carrying `session_id` and the session's own growing `seq`; the
bridge adds a few projections the UI needs that the core does not emit (session info, usage,
title, todo list, the final reply). Tool events are filtered by the session's tool-progress mode,
and their text is redacted and cut so a huge tool output can never flood the pipe or the renderer.

Inputs → outputs:
- `session.create {title?, cwd?, model?, toolsets?, idempotency_key?}` → `{session_id, created}`
  (landed P04 shape; a repeated key returns the first session) + `session.info` event.
- `session.resume {session_id}` (an id, else an exact title) → landed result plus `title` and
  `messages` (display projection: user/assistant/tool rows, multipart content flattened to text with
  `[image]` for image parts, each with its stored `row_id` for undo) + `session.info`.
- `session.list {limit, query?, include_subagents}` → newest first; machine sources and subagents
  hidden by the store's listing rules. `session.most_recent {}` → the first such row or null.
- `session.close {session_id}` → `{closed}` (runtime torn down, the row kept).
- `session.history`, `session.title {title?}` (set or read; emits `session.title`),
  `session.undo {count}` → `{removed, prefill}`, `session.compress {focus?}`,
  `session.branch {title?}` → new session id (the original stays), `session.usage`.
- `prompt.submit {session_id, text, when_busy}` → `{turn_id, generation, status}`:
  `streaming` when a turn started; while a turn runs `when_busy` decides: `reject` (default) →
  `SESSION_BUSY`; `queue` → kept in the bridge and sent when the turn ends (`queued`, ticket of the
  running turn); `steer` → `steered` (falls back to a new turn if the turn ended meanwhile);
  `redirect` → `redirected` / `steered`.
- `prompt.background {session_id, text}` → `{task_id}`: a separate session runs the text; when it
  ends the parent session gets one notice with the reply and the side session is closed.
- `session.interrupt {session_id, generation?, hard}` → `{interrupted}` immediately (never waits for
  an agent being built); a hard interrupt also drops queued prompts.
- `session.steer` → landed `{queued, ticket?}`; `queued=false` is the "rejected" answer (idle).
  `session.redirect` → `redirected | steered | rejected`.
- Events forwarded: `turn.started` (= message start), `message.delta`, `reasoning.delta`,
  `tool.started`, `tool.progress`, `tool.completed`, `status`, `notice`, `turn.done`,
  `server_request.cancelled`, goal/loop/heartbeat updates. Added events: `session.info`,
  `session.usage`, `session.title`, `todo.updated`, `message.complete {turn_id, text, reason}`.

Files: `src/indus/tui_bridge/methods_session.py`, `methods_prompt.py`, `events.py`,
`tests/contract/tui_bridge/test_methods_session.py`, `tests/contract/tui_bridge/test_events.py`.

Edge cases / failure modes:
- create twice with the same idempotency key → one session (service rule); different params with the
  same key → `IDEMPOTENCY_CONFLICT`.
- resume of an unknown id/title → `SESSION_NOT_FOUND`; held by another process → `SESSION_BUSY`.
- title on a session with no stored row yet → kept and written after the first turn.
- undo with nothing to undo → `removed: 0`, empty prefill; on a memory store → `STORE_FAILED`.
- tool-progress modes: `off` drops tool events except failures and tools whose card is the
  deliverable (clarify, write_file, patch, todo_list); `new` drops a start (and its completion) when
  the same tool ran just before in the turn; `all` sends all; `verbose` sends all with longer text.
- verbose text: redacted first, then cut to the last 1,000 characters / 16 lines starting at a line
  boundary, with a leading `[… N lines / M chars omitted]` note. Non-verbose results: first line,
  160 characters. Arguments: strings capped (200 / 1,000 characters).
- `todo.updated`: from a successful `todo_list` completion's JSON (whole list + revision); a result
  that does not parse is ignored.
- a listener error never reaches the turn (the sink logs it).

Our design differences / decisions:
- landed P04 event names are kept (`turn.started` is the message start, `tool.started` /
  `tool.completed`, `status`, `notice`, `turn.done`); the reference's `message.start`,
  `tool.start`, `status.update`, `error` names are not duplicated. `message.interim` and
  `tool.generating` are not emitted: the core has no commentary sealing or argument-drafting signal
  yet (later; the TUI ignores absent events).
- `message.complete` carries the reply text after `turn.done` so a client can reconcile its streamed
  buffer without re-rendering fragments (R05.6).
- busy handling lives in the bridge (`when_busy`); the landed `SESSION_BUSY` stays the default.
- params keep the P04 names (`idempotency_key`, not `request_id`); launch flags (`provider`,
  `skills`, `max_turns`) are P12-32's to add when the launcher maps them.

Tests we will write:
- fake model + in-process service on a real P02 store: create twice with one key → one session;
  stream events validate and carry growing seq; resume flattens image parts to `[image]`; resume by
  title; list hides subagent rows; most_recent; close keeps the row; undo; branch; title event.
- interrupt of an idle / building session returns at once; steer on idle → not queued; queue while
  busy → sent after the turn; redirect outcomes.
- verbose cut at a line boundary with the omitted note; non-verbose head; mode `off`/`new`
  filtering; todo projection; every emitted payload validates against its model.

## Decisions for the lead
- `PromptSubmitParams.when_busy` and `PromptSubmitResult.status` (default `streaming`) added to the
  landed P04 models (additive); `SessionResumeResult` gained `title` and `messages` (default empty).
- `prompt.submit` with `queue`/`steer` returns the running turn's id and generation as the ticket.
- `image.attach` (P12-31) stages files; `prompt.submit` appends one line per staged image naming its
  path (the model reads it with its vision tool) because `SessionService.submit` takes text only.

Compare checklist (filled after the rewrite; reopened `methods_session.py` handlers,
`methods_session_interrupt.py`, `tool_progress.py`):
- [x] idempotent create (test: `tests/contract/tui_bridge/test_methods_session.py::test_create_twice_with_one_key_gives_one_session`)
- [x] listing hides subagent and machine sources (test: `test_methods_session.py::test_list_and_most_recent_hide_machine_sessions`)
- [x] most_recent folds errors into null (test: `test_methods_session.py::test_list_and_most_recent_hide_machine_sessions`)
- [x] interrupt never waits for an agent build (test: `test_methods_session.py::test_interrupt_returns_at_once`)
- [x] idle steer is rejected, the client sends it as a prompt (test: `test_methods_session.py::test_steer_on_idle_is_rejected`)
- [x] verbose tail cut at a line boundary with an omitted note (test: `tests/contract/tui_bridge/test_events.py::test_verbose_text_is_cut_at_a_line_boundary`)
- [x] todo snapshot from the todo tool's result (test: `test_events.py::test_todo_completion_projects_the_list`)
- [x] redaction before cutting (test: `test_events.py::test_verbose_result_is_redacted_before_cut`)
- [x] steer/redirect during compression → queued — gap found; fixed: `when_busy=steer` while the
  status is `compressing` queues instead (pinned by `test_methods_session.py::test_steer_during_compression_is_queued`)
- [x] gap found: a quiet mode hid failed commands whose call "succeeded" (JSON result with an error
  or a non-zero exit code) — fixed by `events.failed_result`, pinned by
  `test_events.py::test_modes_filter_tool_events`
- [x] gap found: an unwritten todo list (`[]`, revision 0) was projected — now skipped, pinned by
  `test_events.py::test_todo_completion_projects_the_list`
- [x] queued prompt runs after the turn (test: `test_methods_session.py::test_busy_submit_rejects_or_queues`)
- Deliberate differences: the reference nests a payload under `params.payload` with a catch-all
  `event` method; ours is one notification per event type with the whole model as params (P04). The
  reference sends full parsed tool results to non-verbose clients; we send a one-line preview.
- Not taken: compute-host interrupt recovery, hosted-room tasks, voice/TTS barge-in, inline diff
  previews (no edit snapshot in the core yet), tool labels.

## Status

Done (2026-10-19).
