# Task: Stream consumer, typing and tool progress (P15-16) Phase: 15 Owner: wave-1 builder G3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/stream_consumer.py` (queue
sentinels, cadence decision, overflow seal, first-send split, approval boundary, finalize paths,
final-text adoption and the delivered-final reconciliation), `gateway/stream_consumer_transport.py`
(send-or-edit order, first send, edit of the live preview, flood backoff and strikes, fallback
mode), `gateway/run_turn_progress.py` (progress callback modes, line building, dedup counter,
accumulate vs separate bubbles, overflow roll, edit throttle), `gateway/run_turn.py`
(`_run_agent_notify_long_running` only), `gateway/run.py` (`_interim_metadata` only). Research
`06-gateway.md` §3.14, traps R06.12, R06.13. Closed before writing.
Evidence: none.

What it does (own words): while the agent writes, the chat shows one message that grows by edits
instead of a burst of messages at the end. Edits go out when 0.8 s have passed with new text or 24
new characters are waiting; a cursor glyph marks a message still being written. When the text
outgrows one message the current message is sealed with the head and the rest continues in a new
one. A tool call or an approval prompt seals the current message too, so later text lands below the prompt. If the platform
cannot edit, each sealed segment goes out once. The final reply is reconciled against what is
already on screen: the live message is edited to the final text, and a new message is sent only
when nothing can be edited (no duplicate answer). Flood refusals stretch the edit interval (up to
30 s) and three in a row stop interim edits; the final then goes out as one message with only the
missing tail. Every send or edit that is not the final carries the interim mark (R06.12). Tool
progress shows tool activity (`off/new/all/verbose/log`, one edited bubble or one per tool); `log`
writes a redacted rotating audit file instead. Typing is refreshed every 2 s and paused while an
approval waits. A "still working" heartbeat appears on long turns.

Inputs → outputs:
- `StreamConsumer(port, chat_id, *, clock, settings, redact, reply_to, meta, on_new_message)`:
  `delta(text)`, `tool_boundary()`, `approval_boundary() -> awaitable`, `finish(final_text)`,
  `feed(event)` (protocol events: `message.delta` → delta, `tool.started` → boundary,
  `turn.done` → finish), `run()` (the task), `result` (`StreamOutcome`: delivered, message ids,
  `final_delivered`, `needs_final_send`).
- `StreamSettings { edit_interval 0.8, buffer_threshold 24, cursor "▉", max_backoff 30, flood_strikes 3 }`;
  `streaming_default(platform)` (telegram on, discord off, slack off).
- `ToolProgress(port, chat_id, *, clock, mode, grouping, redact, audit)`, `TypingLoop(port, chat_id,
  *, clock, interval=2.0)` with `pause()`/`resume()`/`stop()`, `Heartbeat(port, chat_id, *, clock,
  interval=180)`, `progress_defaults(platform)`, `AuditLog(path)` (5 MB × 3, redacted).

Files: `src/indus/gateway/outbound/stream.py`, `src/indus/gateway/outbound/progress.py`,
`src/indus/gateway/outbound/ports.py` (the one place adapter calls are made: interim/final
marking and redaction at both doors), `tests/unit/gateway/outbound/test_stream.py`,
`tests/unit/gateway/outbound/test_progress.py`.

Edge cases / failure modes:
- interim frames show text only up to the last whitespace: a half-streamed token is never shown,
  so the redactor always sees whole secrets.
- a buffer that could still become a silence token is held back (no edit) until it diverges.
- media tags are stripped from every frame (media go out after the final, P15-18).
- the edit result names an undelivered tail (`continuation_ids` / failure) → fallback: only the
  missing tail is sent at the end.
- first send fails → edits off for this stream; the final goes out once.
- edit result skipped because the per-chat slot is busy (P15-05) → counts as success, retried later.
- approval boundary with nothing written in the segment → nothing is sent (the placeholder is a
  native-stream feature, P2).
- final text that extends the streamed text (post-stream augmentation) → rides the final edit;
  final text unrelated to sealed heads → sent as a new message.
- turn cancelled → best-effort final edit without cursor.

Our design differences / decisions:
- Single asyncio task fed through a thread-safe inbox; deadlines come from the injected clock.
- Interim mark is metadata key `interim: True`; the final carries `final: True` (adapters strip
  both before the wire; P15-05/33–35).
- Progress lines are built from the tool name and a fixed verb table; `verbose` adds the P05
  redacted argument JSON (truncated). Raw arguments never appear.
- Heartbeat interval 180 s, edited in place, our wording.
- No native draft/stream transports (P2 per phase); frames are not prefix-stable on purpose
  (the edit transport closes fences per frame).

Tests we will write: fake clock: edit cadence (interval and threshold); overflow seals and
continues; edits unsupported → one message per sealed segment; approval seals the live message;
3 floods disable edits and the final sends only the tail; interim flag on every non-final
send/edit; final reconciliation edits instead of a duplicate send; augmentation rides the final;
partial token held back; typing refresh every 2 s and paused during approval; progress modes
(off/new/all/verbose/log), accumulate vs separate, dedup counter, audit file rotation and
redaction; heartbeat edited in place.

## Decisions for the lead
- Interim frames hold back the trailing partial word (secret safety); the final shows all.
- Progress edit throttle 1.5 s (reference value; the per-chat slot of P15-05 also applies).
- Config rows not added (gateway section belongs to P15-02): `platforms.<name>.streaming`,
  `display.tool_progress_grouping`, `display.tool_progress` per platform, `display.heartbeat_seconds`
  are read from plain settings objects here; the lead wires them to the schema at integration.

Compare checklist (filled after the rewrite; reopened `gateway/stream_consumer.py`,
`gateway/stream_consumer_transport.py`, `gateway/run_turn_progress.py`, the heartbeat in
`gateway/run_turn.py`):
- [x] cadence 0.8 s / 24 code points, first message waits for 4 visible characters (`tests/unit/gateway/outbound/test_stream.py::test_edits_follow_interval_and_threshold`)
- [x] overflow seals and continues, fence carried (`::test_overflow_seals_and_continues_in_a_new_message`, `::test_overflow_carries_an_open_fence`)
- [x] no edit support: one message per sealed segment (`::test_without_edit_support_each_sealed_segment_is_one_message`)
- [x] flood strikes, backoff cap, final sends only the missing tail (`::test_three_floods_stop_edits_and_final_sends_tail`, `::test_flood_backoff_is_capped_at_30_seconds`)
- [x] interim marking at both doors (`::test_every_non_final_send_is_marked_interim`)
- [x] final reconciliation, augmentation rides the final (`::test_final_edits_the_stream_message_instead_of_sending_again`, `::test_unrelated_final_after_sealed_heads_goes_below`)
- [x] approval boundary seals the live message, later text below (`::test_approval_seals_and_resumes_below`)
- [x] silence prefixes held, token never shown (`::test_silence_token_is_never_shown`)
- [x] first-send failure → edits off, final once (`::test_first_send_failure_stops_edits_and_final_goes_out_once`)
- [x] typing 2 s, paused during approval (`test_progress.py::test_typing_pauses_while_approval_waits`)
- [x] progress modes, grouping, dedup counter, overflow roll, audit log (`test_progress.py`, 12 tests)
- [x] gap found: after edits stop, the reference takes the cursor off the stuck preview once →
  `_strip_cursor` on detach (pinned in `::test_three_floods_stop_edits_and_final_sends_tail`).
- [x] gap found: a repeat counter after a closing fence breaks the fence → own line
  (`test_progress.py::test_repeat_counter_after_a_fence_gets_its_own_line`).
- Deliberate differences: the approval placeholder belongs to native streams (the reference uses
  it only there); with the edit transport the boundary seals the live message and an empty
  segment shows nothing. Interim frames hold the trailing partial word. Backoff doubles up to the
  30 s cap (the reference caps doubling at 10 s and server waits at 30 s).
- Not taken: native draft/stream transports, fresh-final for long previews, progress cleanup after
  success, Slack task cards, think-tag filtering (the service already separates reasoning).

## Status

Done (2026-10-19). Interfaces through `outbound/ports.py`; wiring listed in the builder report.

Integration (lead): `ports.py` now calls G1's real `Adapter` (capabilities `max_len`, `len_fn`,
`markup`, `supports_edit`, `supports_delete`; chat-level not found from `SendResult.chat_level`;
media through `send_image/video/voice/document`, audio as `send_file`, an unsupported kind falls
back to a document). The base strips `interim` / `final` before its hooks and treats a `final`
mark on an edit as a final edit; a skipped interim edit is `ok` with `skipped`. Config rows:
`platforms.<name>.streaming` / `tool_progress` (unset = platform default, read through
`PlatformSettings.streaming` / `.tool_progress`), `display.tool_progress_grouping`,
`display.heartbeat_seconds`, `display.ephemeral_system_ttl`. Tests use
`tests/unit/gateway/outbound/recording.py` (a recording adapter on the real base).
