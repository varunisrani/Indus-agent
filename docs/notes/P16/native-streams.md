# Task: Native draft streaming for Telegram and Slack (P16-33) Phase: 16 Owner: wave-28 builder E16

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/stream_consumer.py` (native
and draft branches: transport choice, seed, segment/boundary handling, final adoption, cancel),
`gateway/stream_consumer_transport.py` (send-or-edit order, native push, draft push),
`plugins/platforms/slack/adapter.py` (native stream calls only: probe, frame, start, stale seal,
stream key, final relation, seal, commit), `plugins/platforms/telegram/adapter.py` (draft probe and
`send_draft` only). Research 06 §3 (Telegram row), R06.12, R06.13. Evidence: none.
Interop (vendor): Telegram Bot API `sendMessageDraft` (`chat_id` of a private chat, optional
`message_thread_id`, non-zero integer `draft_id` — frames with the same id animate one draft —,
`text` ≤ 4,096 UTF-16 units, optional `parse_mode`; answers `true`; a draft is a preview, the reply
itself is a normal `sendMessage`); Slack Web API through slack-sdk 3.44.1 (our pin; signatures read
from the installed wheel): `chat.startStream(channel, thread_ts, markdown_text?, recipient_user_id?,
recipient_team_id?)` → `ts`, `chat.appendStream(channel, ts, markdown_text)` (appends),
`chat.stopStream(channel, ts, markdown_text?)` (appends, then seals); stream text is standard
Markdown, not mrkdwn; a sealed message refuses appends with `message_not_in_streaming_state`;
an app without the Agents & AI Apps feature gets `not_allowed` / `missing_scope` /
`feature_not_enabled` / `unknown_method` style errors; rate limits are `ratelimited` + `Retry-After`.
Telegram's draft method could not be checked against the live docs offline: the names above follow
the Bot API changelog as we know it (9.x) and the reference; live check owed (P16-36 / owner).

What it does (own words): with native streaming on for a platform, the reply is shown while it is
written through the platform's own streaming primitive instead of a message we keep editing.
Telegram shows an animated draft in a private chat (`sendMessageDraft` with one draft id per
stream); the real reply is then sent once, normally, and replaces the draft. Slack opens a stream
message in the thread (`chat.startStream`), appends what is new (`chat.appendStream`) and seals it
(`chat.stopStream`): the stream is the reply, nothing is posted a second time. Both only ever send
frames that extend what they sent before (no fence closing, cursor or markup conversion per frame,
R06.12): a frame that would rewrite sent text is skipped and the final carries it. The final is
declared by the caller; text added after streaming (footers, notes) rides the final (Slack: the
delta goes in the seal; Telegram: the final message holds all of it). A final that arrives for a
stream already sealed is reconciled by editing the sealed message, never by posting again. Flood
answers during a stream hold interim frames back (the server's wait, doubling, capped; three in a
row stop interim frames) without sleeping; the final is never held back.

Inputs → outputs: `display.platforms.<p>.native_stream: true` (default false) →
`TelegramAdapter.open_stream(chat_id, reply_to=, meta=)` → `DraftStream` (private chats only) and
`SlackAdapter.open_stream(...)` → `SlackStream` (needs a thread: `meta["thread_id"]`, else the
message replied to); `push(text)` with the whole text so far → frames; `finish(text)` →
`SendResult` (the final's message id); `abandon()` on a cancelled turn.

Files: `src/indus/gateway/adapters/_native.py` (flag reader, flood gate, the stream shape),
`src/indus/gateway/adapters/telegram/drafts.py`, `src/indus/gateway/adapters/slack/streams.py`,
`tests/unit/gateway/adapters/test_native_streams.py`, `tests/fixtures/gateway/native_stream_frames/`;
hook points in `telegram/adapter.py`, `telegram/__init__.py`, `telegram/bot_api.py`
(`send_message_draft`), `slack/adapter.py`, `slack/__init__.py`; shared: the wire fakes
(`telegram_wire.py`, `slack_wire.py`), `test_vendor_names.py` (documented method list), config
schema row `display.platforms.<p>.native_stream` + its test, traps R06.12, ACC-K28-2.

Edge cases / failure modes:
- a frame that does not extend the last one (a rewritten interim) → skipped, carried by a later
  frame or the final; an identical frame → no call;
- Telegram text over 4,096 UTF-16 units → drafts freeze at what was shown; the final send splits;
- Slack total over the 39,000 cap → no more appends; the final seals with what fits (cut at a line
  break or space) and the rest goes out as ordinary thread messages (mrkdwn);
- flood (`RetryAfter` / `ratelimited`) → interim frames held until the wait passes (doubling, capped
  at 30 s); three in a row → interim frames stop; never slept inline;
- the shared per-chat slot (R06.13): a Telegram draft frame is skipped while a send holds the slot;
- a frame refused for another reason → the stream is dead (`push` False): the caller falls back to
  edits; Slack feature errors switch native streams off for the adapter (logged once); a Telegram
  refusal naming the method or the chat (drafts unsupported) switches drafts off;
- Slack `message_not_in_streaming_state` mid-stream → a fresh stream opens in the same thread with
  only the text past the sealed message;
- second `finish` with the same text → the first result, no call; with other text → an edit;
- Slack seal fails → one retry when nothing was appended, then an in-place `chat.update` with the
  whole final; both fail → a failure result (the caller sends normally: a duplicate beats a loss);
- a final unrelated to the streamed text (Slack) → the stream is sealed and edited to the final;
- `abandon()` (turn cancelled): Slack seals the open stream; Telegram drafts expire on their own;
- no thread (Slack), a group chat (Telegram), not connected, send-only, ignored channel → no stream.

Our design differences / decisions:
- our names (`DraftStream`, `SlackStream`, `FloodGate`, `NativeStream`); one shape for both
  (`push` / `finish` / `abandon`, `frames`, `one_message_per_turn`) matching WeCom's `open_stream`;
- the switch is `display.platforms.<p>.native_stream` (the phase row), read when the adapter is
  built; WeCom keeps its own `platforms.wecom.native_stream` (on by default) from P16-17;
- no "rich" Telegram drafts, no seed frame, no tool-progress overlay inside the stream, no Slack
  task-card / plan streams, no stale-stream sweeper (one stream object per turn; `abandon` seals);
- Telegram draft frames are plain text (MarkdownV2 escaping and entity closing are not
  prefix-stable); the final is MarkdownV2 with the plain fallback, like every other send;
- the core consumer has no native branch and the runner does not feed `StreamConsumer` yet: the
  consumer change is proposed to the lead (below), not made here.

Tests we will write: recorded frame sequences (fixtures) replayed through both fakes: every frame
a prefix of the next, the fence never closed mid-stream, no cursor; augmentations ride the final;
no duplicate final after seal (second finish, sealed stream + new final → edit); flood backs off
on the fake clock and three floods stop interim frames; feature-off fallbacks; Slack server seal
→ reopen; overflow spills; the flag off → no stream.

## Decisions for the lead
- **Core change proposed (not made): the consumer's native branch.** See the report's exact diff
  (`StreamConsumer(native=...)`): when the adapter offers `open_stream` and returns a stream,
  interim updates push the raw redacted text up to the last whitespace (media tags removed; no
  fence closing, cursor or formatting) instead of editing; a tool boundary finishes the segment and
  opens a new stream unless `one_message_per_turn` (Slack); an approval boundary finishes the
  stream; `finish(final)` declares the final (augmentations ride it); a `push` returning False or a
  `finish` that is not ok falls back to the edit/send path for what is left; cancel calls
  `abandon()`. Plus `Adapter.open_stream` default returning None. The runner must feed the
  consumer (open seam since P15 wave 22).
- The flag lives under `display.platforms.<p>` as the phase row says, beside `platforms.<p>.streaming`;
  a new config row (`DisplaySection.platforms`, one model with `native_stream`).
- `streaming_default("wecom")` and a WeCom `abandon()` are left to the core change.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] prefix-stable frames; cursor, fence closing and markup conversion never in a frame
  (test: `test_native_streams.py::test_recorded_telegram_draft_frames_are_prefix_stable`,
  `::test_recorded_slack_stream_frames_are_prefix_stable`, `::test_a_rewriting_or_repeated_frame_is_skipped`)
- [x] final declared by the caller; the augmentation is the seal's delta (Slack) / part of the one
  final message (Telegram) (same tests)
- [x] sealed stream + another final → an edit, never a second post (`::test_no_duplicate_final_after_seal`,
  `::test_a_final_unrelated_to_the_stream_seals_and_edits`)
- [x] seal retried only when nothing was appended, else settled by `chat.update`
  (`::test_a_failed_bare_seal_is_retried_once`, `::test_a_failed_seal_is_settled_by_one_edit`)
- [x] Slack feature errors latch native off (`::test_slack_without_ai_streaming_switches_native_streams_off`);
  `message_not_in_streaming_state` reopens with the text after the sealed message
  (`::test_a_stream_slack_sealed_continues_in_a_fresh_one`)
- [x] Telegram drafts private chats only, refusals switch drafts off (inside topics only when a
  topic refused), 4,096 UTF-16 cap (`::test_drafts_only_in_private_chats_and_only_when_switched_on`,
  `::test_a_refused_draft_ends_the_stream_and_switches_drafts_off`,
  `::test_a_topic_refusal_turns_drafts_off_inside_topics_only`,
  `::test_text_over_the_draft_cap_freezes_the_draft_and_the_final_splits`)
- [x] flood backs off without sleeping; three in a row stop frames
  (`::test_a_flood_during_a_stream_backs_off_without_sleeping`); drafts share the send slot (R06.13,
  `::test_draft_frames_share_the_chats_send_slot`)
- [x] gap found: a tail too long for one append — the reference seals what is visible and lets the
  plain path post the whole final (visible text twice); we seal with what fits and send only the
  rest (`::test_a_stream_past_the_cap_seals_what_fits_and_the_rest_follows`)
- Deliberate differences: plain-text Telegram drafts (the reference converts each frame to
  MarkdownV2 with a plain retry, which is not prefix-stable and can be refused mid-stream); no
  rich drafts; frames over the cap freeze instead of being truncated; a lost (timeout) Slack
  append stalls the frames and lets the final edit the message instead of guessing what landed;
  no stale-stream sweeper (the stream object lives for one turn; `abandon` seals); recipient
  user/team passed when known.
- Not taken: Slack task-card / plan streams, tool-progress overlay inside the stream, seed frame
  ("typing" bubble before the first token), fresh-final replacement for long previews, clarify
  reopen seeding (the consumer branch opens lazily).

## Proposed core change (exact diff in the builder report)

`Adapter.open_stream(chat_id, *, reply_to=None, meta=None) -> NativeStream | None` (default None);
`ports.open_native_stream`; `StreamConsumer` native branch (`_native_update`, `_native_segment_end`,
`_native_finish`, abandon on cancel; interim pacing unchanged, raw redacted frames); WeCom's
`open_stream` gains `meta`, `WeComStream` gains `one_message_per_turn = True` and `abandon()`.
Verified by applying it to this worktree, running `tests/unit/gateway/outbound`, the WeCom tests,
this task's tests and two end-to-end consumer checks (Telegram drafts across a tool call, Slack one
stream across a tool call with the augmented final), then removing it again.
Note for the lead: `WeComStream.push` answers False for a frame that does not extend the last one;
under the proposed branch False means "fall back to edits", so WeCom should answer True there (the
consumer only pushes growing text, so this does not arise in practice).

## Status

Done (2026-10-20) for the adapter side; the consumer branch and the runner wiring are with the
lead (proposal above). Shared files touched: config schema row + test, the Telegram and Slack wire
fakes, `test_vendor_names.py` (two documented Bot API methods), traps R06.12 / R06.13, ACC-K28-2.

Integration (2026-10-20, P16 integrator): the proposed consumer branch was applied as written
(`Adapter.open_stream`, `ports.open_native_stream`, the `StreamConsumer` native branch, WeCom's
`open_stream(meta)`, `one_message_per_turn` and `abandon`), and `WeComStream.push` now answers True
for a frame that does not extend the sent text (skipped; the stream stays usable, as `NativeStream`
says). Consumer-level tests in `tests/unit/gateway/outbound/test_stream_native.py` (scripted stream:
raw prefix frames, segment and whole-turn streams, approval seal, give-up and failed-final fall back
to edits, cancel abandons, a raising `open_stream` streams by edits; and the builder's end-to-end
check over the Telegram draft and Slack stream fakes). D-16.568, D-16.569. The runner still has to
feed the consumer.
