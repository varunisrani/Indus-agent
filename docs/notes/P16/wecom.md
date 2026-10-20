# Task: WeCom smart-robot adapter (P16-17) Phase: 16 Owner: wave-25 builder E8

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/wecom/adapter.py`
(all: settings, connect and subscribe handshake, listen and reconnect loop, heartbeat, frame dispatch,
callback normalisation, DM/group policy, passive vs proactive send, kicked-connection event),
`wecom/streaming.py` (stream turns, per-req_id ack tracking, frame cap, byte truncation, safe-duration
fallback, identical-final workaround), `wecom/send_queue.py` (per-chat FIFO and 30/min bucket),
`wecom/media.py` (skimmed: upload commands, size limits, inbound decrypt), `wecom/gateway_env.py`;
test names in `tests/gateway/test_wecom*.py`, `test_stream_consumer_wecom_native.py`. Research 06 §3.3
(row WeCom). Evidence: none.
Interop (vendor): WeCom smart-robot long-connection document (developer.work.weixin.qq.com path
101463): `wss://openws.work.weixin.qq.com`; frames `{cmd, headers.req_id, body}`, answers
`{headers.req_id, errcode, errmsg}` with no message id; `aibot_subscribe {bot_id, secret}`,
`aibot_msg_callback`, `aibot_event_callback` (`disconnected_event` when another connection takes
over), `aibot_respond_msg` (same `req_id` as the callback; `msgtype` `stream` with `stream.id`,
`finish`, `content`; `markdown`; media by `media_id`), `aibot_send_msg {chatid, chat_type 1/2/0,
msgtype markdown|file|image|…}`, `ping` about every 30 s, `aibot_upload_media_init|chunk|finish`
(≤100 chunks of ≤512 KB, image/video ≤10 MB, voice ≤2 MB AMR, file ≤20 MB, ≥5 bytes), 30 messages a
minute per conversation, stream finished within 10 minutes; callback bodies `msgid`, `chatid`,
`chattype` (`single`/`group`), `from.userid`, `msgtype` `text|image|mixed|voice|file|video`, media
`url` + `aeskey` (URL valid 5 min). WeCom global error codes (path 90313) for the readings below.

What it does (own words): a WeCom "smart robot" in API mode keeps one WebSocket to WeCom (a second
connection kicks the first). After `aibot_subscribe` with the bot id and secret, user messages arrive
as `aibot_msg_callback` frames; each carries a `req_id` that the bot uses to answer that message
(`aibot_respond_msg`), and the answer can be a native stream: frames with one `stream.id`, cumulative
content, `finish: true` on the last. A conversation can also be written to proactively
(`aibot_send_msg`) once the person has talked to the bot. The adapter keeps the socket alive with a
`ping` every 30 s; a ping that gets no answer in time means the socket is dead and it reconnects
(2, 5, 10, 30, 60 s backoff). Connect gives up after 20 s, each request after 15 s. Message ids seen in
the last 1,000 are dropped as duplicates.

Inputs → outputs: `AdapterConfig` (`WECOM_BOT_ID`, `WECOM_SECRET`; `platforms.wecom.websocket_url`,
`dm_policy`, `group_policy`, `allow_from`, `group_allow_from`, `native_stream`) → connected adapter;
callback frames → `InboundEvent`s; `send` → `aibot_respond_msg` markdown when a recent `req_id` for
the chat is known, else `aibot_send_msg`; `open_stream(chat, reply_to)` → `WeComStream`
(`push(text)` interim, `finish(text)` final); media → chunked upload then a media message;
attachments (`wecom://media/<token>`) → downloaded and AES-decrypted bytes.

Files: `src/indus/gateway/adapters/wecom/__init__.py` (two factories: `create_adapter`,
`create_callback_adapter`), `.../wecom/adapter.py`, `.../wecom/stream.py`,
`tests/fakes/platform/wecom_wire.py`, `tests/contract/gateway/wecom_case.py`,
`tests/unit/gateway/adapters/wecom/test_adapter.py`; shared: `pyproject.toml` (extra `wecom`),
`THIRD_PARTY_NOTICES.md`, config schema row `platforms.wecom` + test, contract `conftest.py`, the
contract suite's `lacks` shapes (below), traps ledger R06.12 row.

Edge cases / failure modes:
- missing bot id / secret → fatal; httpx-ws missing → fatal `missing_dependency`; subscribe answered
  with an error code → fatal at connect (`auth_failed`), `report_fatal("wecom_auth")` on a reconnect;
- `disconnected_event` (another connection took over) → `report_fatal("wecom_replaced")`, no reconnect
  (reconnecting would kick the other gateway, which would kick us back);
- a missed ping answer (15 s) → socket closed and reopened; pending requests fail as "maybe delivered"
  (the frame left) or retryable (it never left);
- duplicates by `msgid` (last 1,000); frames that are not JSON objects → dropped;
- groups: the leading `@bot` mention is cut so `/approve` works; group messages count as mentions;
- send to a group with no known `req_id` → proactive send with `chat_type` 2; DM → 1; unknown → 0;
- per-chat 30 sends a minute: an exhausted window answers rate limited with the wait (no sleep, R06.3);
- stream frames: prefix-stable (an interim that does not extend the last frame is skipped), at most 85
  interim frames, ≤20,480 UTF-8 bytes per frame (cut on a character boundary), interim frames skipped
  while the previous one is unacknowledged, the final waits for the pending ack, an identical final
  gets a zero-width space so WeCom does not drop it, a stream older than 330 s refuses the final (the
  caller sends normally); expired-stream codes 846604 / 846608 end the stream;
- error readings: 846607 / 45009 rate limited (the wait from the message when stated, else the
  window's), 93006 / 86004 / 40003 chat not found, 846604 message to reply not found, 60011 / 48002
  forbidden, 40058 / 40008 / 44004 can't parse, 45002 too long; a request with no answer in 15 s is
  a read timeout (never retried).

Our design differences / decisions:
- our names (`WeComAdapter`, `WeComStream`, `ChatWindow`, `WeComError`); httpx-ws instead of aiohttp
  (`wecom` extra: `httpx-ws==0.9.0`, and `cryptography==50.0.2` for media and the callback crypto,
  both already locked);
- message ids: WeCom answers no id, so a proactive send is named by its `req_id`, the n-th reply to
  one inbound message `<req_id>:<n>`, a stream by its `stream.id` (the wire fake files messages under
  the same names);
- one per-chat sliding window (30/min) instead of two token lanes; sends to one chat are already
  serialised by the base adapter's send slots;
- no text batching of split long messages (P15 core has none for other platforms either); no reply
  quoting of quoted messages beyond the text;
- template cards for approvals not built (text fallback with /approve hints);
- the native stream is exposed as `open_stream` (on by default, `native_stream: false` turns it off);
  the core runner has no native-stream seam yet (P16-33): wiring owed.

Tests we will write: the shared contract; every stream frame is a prefix of the next (and a
non-extending interim is skipped); missed heartbeat reconnects (fake clock); duplicate id ignored;
kicked connection is fatal and not retried; subscribe refusal fatal; passive vs proactive routing;
rate window; media upload chunking and size limits; inbound media decrypt round trip; `indus send` path.

## Decisions for the lead
- Policy defaults: `dm_policy: pairing` (the reference's), `group_policy: open` — "open" here means
  the adapter forwards group messages and the core's chain decides (its `group_allow_from` names
  allowed senders); the reference's group `pairing` default with `group_allow_from` as chat ids does
  not map onto our core.
- Contract `lacks` gains `voice_media` (the voice note arrives transcribed, no attachment),
  `document` and `caption` (WeCom callback mode has neither) — a shared test change.
- Registry rows (P15 core, for the lead): `_chat_entry("wecom", "WeCom", sdk=("httpx_ws",),
  extra_name="wecom", required_env=("WECOM_BOT_ID", "WECOM_SECRET"), max_len=4000, hint=...)` with
  `owns_access_policy` handled by the adapter's capabilities; `BUILTIN_CREDENTIALS["wecom"] =
  ("WECOM_BOT_ID", "WECOM_SECRET")`.
- `outbound.stream.streaming_default("wecom")` should be True once the runner feeds native streams.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] connect 20 s, request 15 s, heartbeat 30 s, backoff 2/5/10/30/60; a missed ping reconnects
  (test: `wecom/test_adapter.py::test_a_missed_heartbeat_reconnects`)
- [x] dedupe 1,000 ids (`::test_duplicate_message_ids_are_ignored`); group mention cut and DM/group
  policies (`::test_group_mentions_are_cut_and_policies_apply`); quotes as reply context
  (`::test_quotes_become_reply_context`)
- [x] passive reply first, proactive fallback, an expired anchor falls back
  (`::test_replies_are_passive_when_possible_else_proactive`); 846609 drops the anchors
  (`::test_a_lost_subscription_drops_the_reply_anchors`)
- [x] stream: interim cap 85, byte cap, pending-ack skip, final drains the pending answer, identical
  final, safe age, prefix stability (`::test_every_stream_frame_is_a_prefix_of_the_next`,
  `::test_unanswered_interims_are_skipped_and_the_final_carries_the_text`,
  `::test_an_identical_final_still_differs_and_long_finals_spill_over`,
  `::test_an_old_stream_refuses_its_final_so_the_caller_sends_normally`)
- [x] kicked connection: reconnect once, then fatal
  (`::test_a_takeover_reconnects_once_then_two_within_ten_minutes_are_fatal`)
- [x] media limits and chunking (`::test_media_goes_up_in_chunks_with_wecom_limits`); inbound media
  decrypt (`::test_inbound_media_is_downloaded_and_decrypted`)
- [x] 30 messages a minute per chat, the wait returned not slept (`::test_thirty_messages_a_minute_per_chat`)
- [x] shared contract over the wire fake (`tests/contract/gateway/wecom_case.py`)
- Deliberate differences: httpx-ws instead of aiohttp; a takeover reconnects once after 60 s
  (an `indus send` borrowed the bot) and only a second one within 10 minutes is fatal (the reference
  never reconnects); groups without a cached `req_id` get a proactive send with `chat_type` 2/0 (the
  vendor allows it once the person wrote; the reference refuses); one 30/min window instead of
  24 + 6 reserved tokens; a long final spills its rest into ordinary messages instead of being cut;
  no seed frame (`<think></think>`) and no keep-alive frames (the reference keeps those off by
  default too); group allowlists are the core's (`group_allow_from` names senders there).
- Not taken: text batching of client-split long messages and the attachment/text merge window,
  `force_proactive` / control lane for approval prompts, per-group `groups.<id>.allow_from`, template
  cards, QR setup (P16-31), the private-address guard on media downloads (URLs come from the
  authenticated socket; follow-up).

## Status

Done (2026-10-20). New files: `wecom/__init__.py`, `adapter.py`, `stream.py`, the wire fake (shared
with P16-18), contract case, unit tests. Shared: `pyproject.toml` (extra `wecom`),
`THIRD_PARTY_NOTICES.md`, 00-layout §4, config schema row `platforms.wecom` + test, contract
`conftest.py` and the `lacks` shapes in `test_adapter_contract.py` (`voice_media`, `document`,
`caption`), traps ledger R06.12 (two test lines). Wiring owed: the runner has no native-stream seam
(P16-33); `open_stream` / `WeComStream.push` / `finish` are ready for it.

## Landing (integrator, 2026-10-20)

No private-address guard was added to inbound media downloads: neither the P16-01 kit nor `_identity`
has one (the URLs come from the authenticated socket). `indus.safety.safe_transport` is the seam to
use (follow-up). Lock keys: wecom `WECOM_BOT_ID`, wecom_callback corp id + agent id (D-16.454).
