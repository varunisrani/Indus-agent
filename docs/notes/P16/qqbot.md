# Task: QQ Bot adapter (P16-20) Phase: 16 Owner: wave-25 builder E7

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/qqbot/adapter.py`
(all: token, gateway URL, WebSocket loop and close codes, Hello/Identify/Resume/heartbeat, dispatch,
inbound handlers per chat kind, attachments and voice, interactions, keyboards for approvals, REST
send, media upload, typing), `qqbot/chunked_upload.py` (all), `qqbot/keyboards.py`,
`qqbot/constants.py`, `qqbot/gateway_env.py`, `qqbot/utils.py`, `qqbot/plugin.yaml`; skimmed
`qqbot/send.py`, `qqbot/register.py`, `qqbot/crypto.py` (setup-only credential decryption); test
names only in `tests/gateway/test_qqbot.py`. Evidence: none.

What it does (own words): connects Indus Agent to QQ through the official QQ Bot API v2. The bot's
app id and client secret buy an access token (`POST https://bots.qq.com/app/getAppAccessToken`,
refreshed a minute before `expires_in` runs out, one refresh at a time). REST calls go to
`https://api.sgroup.qq.com` with `Authorization: QQBot <token>`. Inbound arrives over the gateway
WebSocket (`GET /gateway` gives its URL): the server says Hello (op 10, heartbeat interval), we
Identify (op 2: token, intents for C2C/group @-messages, public guild @-messages, guild DMs and
button interactions, shard `[0, 1]`) or, after a drop, Resume (op 6: token, session id, last
sequence); READY gives the session id; every dispatch (op 0) carries a sequence number we keep;
heartbeats (op 1 with the last sequence) go at 80 % of the server's interval and an ACK (op 11) must come
back before the next one, else the socket is treated as dead. op 7 (reconnect) closes and resumes,
op 9 (invalid session) closes and resumes only when its data is true, else identifies afresh.

Reconnects wait 2, 5, 10, 30, 60 s (then 60 s); 100 failed attempts in a row → fatal. Close codes:
4004 (bad token) refreshes the token; 4006/4007/4900–4913 drop the session (identify next time);
4008 (rate limited) waits 60 s; 4001/4002/4010–4014/4914/4915 are fatal (bad intents, banned,
sandbox-only…); three closes within 5 s of opening in a row → fatal (usually missing permissions).

Chats, kept apart by an id prefix so a reply or cron target always knows its endpoint:
- C2C (a private chat with a QQ user): `C2C_MESSAGE_CREATE`, chat id = the user openid (bare, the
  DM convention), sends to `/v2/users/{openid}/messages`;
- group: `GROUP_AT_MESSAGE_CREATE` (the bot only gets @-mentions), chat id `group:<group_openid>`,
  user = the member openid, sends to `/v2/groups/{id}/messages`;
- guild channel: `AT_MESSAGE_CREATE` / `GUILD_MESSAGE_CREATE`, chat id `channel:<channel_id>`, sends
  to `/channels/{id}/messages`; author `bot: true` marks `is_bot`;
- guild DM: `DIRECT_MESSAGE_CREATE`, chat id `dm:<guild_id>` (the DM session's guild), sends to
  `/dms/{guild_id}/messages`.
Messages are deduped by id (1,000 ids for 300 s). A leading `@name` is cut from group and channel
text. Attachments (`content_type`, `url`, `filename`; protocol-relative URLs get `https:`) become
`Attachment`s; voice prefers QQ's `voice_wav_url` (already WAV) and keeps QQ's own transcript
(`asr_refer_text`) in `metadata["transcript"]` and as the text when the message has none. Media are
downloaded only from https QQ hosts (`qq.com`, `qpic.cn`, `qq.com.cn`, `gtimg.cn` and their
subdomains), with the bot token, size-capped. Button presses (`INTERACTION_CREATE`) are acknowledged
(`PUT /interactions/{id}`) and become button events carrying the button's data.

Outbound: QQ wants a "passive" reply to name the message it answers (`msg_id`) and a per-message
sequence (`msg_seq`, the same pair is deduped by QQ). We remember the last inbound message per chat
for 5 minutes and send every piece of a reply with it and a growing `msg_seq`; after that window a
send goes out without `msg_id` (an "active" message, which QQ rate-limits). Text cut at 4,000
characters; plain text by default (`msg_type 0`), Markdown (`msg_type 2`) when
`platforms.qqbot.markdown: true` (needs QQ's permission). No edits. Typing (`msg_type 6`
input-notify, 60 s) only in C2C, at most every 50 s, and only with a fresh inbound `msg_id`.
Approvals: an inline keyboard (one row: once / session / always / deny, callback buttons whose data
is our approval callback) in C2C and groups; guild chats get the text card. Media (C2C and groups;
guild → refused): a local file goes through the chunked upload (`upload_prepare` → PUT each part to
its pre-signed URL → `upload_part_finish` → `files` with the upload id), then a `msg_type 7` message
with the returned `file_info`. Parts upload one at a time unless the server allows more (max 10),
300 s per part, 2 retries per part (3 tries); a part-finish answered with the "retry" business code
40093001 is retried every 1 s until the server's `retry_timeout` (default 120 s, max 600 s); the
final `files` call is retried twice (2 s, 4 s). The daily upload limit (40093002) is never retried:
the send fails with a clear "daily limit" error.

Inputs → outputs: `AdapterConfig` (`QQ_APP_ID`, `QQ_CLIENT_SECRET` secrets; `platforms.qqbot.*`:
`markdown`, `sandbox`, `max_media_bytes`) → a connected adapter; gateway frames → `InboundEvent`s;
sends / media / approvals / typing → REST calls → `SendResult`s.

Files: `src/indus/gateway/adapters/qqbot/__init__.py`, `.../qqbot/adapter.py`, `.../qqbot/upload.py`,
`.../qqbot/client.py` (REST + token + WebSocket opener), `.../qqbot/gateway.py` (the WebSocket
session state), `tests/fakes/platform/qqbot_wire.py`, `tests/contract/gateway/qqbot_case.py`,
`tests/unit/gateway/adapters/qqbot/test_adapter.py`, `.../qqbot/test_upload.py`; shared:
`pyproject.toml` (extra `qqbot`), `THIRD_PARTY_NOTICES.md` (httpx-ws / wsproto rows name the
extra), `tests/contract/gateway/conftest.py` (one import line), `foundation/config/schema.py` + its
test (row `platforms.qqbot`).

Vendor API: QQ Bot API v2 — token `POST bots.qq.com/app/getAppAccessToken`; REST `GET /gateway`,
`POST /v2/users/{openid}/messages`, `POST /v2/groups/{group_openid}/messages`,
`POST /channels/{channel_id}/messages`, `POST /dms/{guild_id}/messages`,
`POST /v2/{users|groups}/{id}/files`, `.../upload_prepare`, `.../upload_part_finish`,
`PUT /interactions/{id}`; gateway opcodes 0, 1, 2, 6, 7, 9, 10, 11; events READY, RESUMED,
C2C_MESSAGE_CREATE, GROUP_AT_MESSAGE_CREATE, AT_MESSAGE_CREATE, GUILD_MESSAGE_CREATE,
DIRECT_MESSAGE_CREATE, INTERACTION_CREATE. Sandbox: `https://sandbox.api.sgroup.qq.com`. WebSocket
client: `httpx-ws` 0.9.0 (MIT, needs `wsproto` 1.3.2 MIT; both already locked), new extra `qqbot`,
imported lazily; REST on httpx.

Capability flags (research 06 Q4): `dm_is_private` true (C2C and guild DMs are one-to-one),
`owns_access_policy` false (the core chain with `INDUS_QQBOT_*` lists decides),
`auth_upstream` false, `supports_edit` false, `supports_delete` false, `supports_buttons` true
(keyboards; guild chats fall back to text inside the adapter), `push_capable` true (active
messages, rate-limited by QQ), `splits_natively` true, cap 4,000 code points, markup `plain`
(Markdown passed through when enabled).

Edge cases / failure modes:
- Token endpoint: missing token or credentials refused → fatal connect error; network → retryable.
- REST errors: QQ's JSON `{code, message}`; 429 → rate limited (`Retry-After`), 401 → token
  refreshed once and the call retried, then a final failure; 403 → forbidden; 404 → chat not found;
  read timeout → maybe delivered (never retried); long flood waits are never slept inline (R06.3:
  the core's inline cap hands them to the ledger).
- Frames: non-JSON, non-object, unknown ops and events ignored; a sequence number only grows.
- Resume keeps the last sequence across a drop; a fresh identify only after op 9 (false) or a
  session-invalid close code.
- Heartbeat ACK missing at the next beat → close and resume.
- Duplicate message id → dropped. Messages with no text and no attachment → dropped.
- Chunked upload: a short read of the file, a part PUT failing three times, part-finish timing out,
  a missing `upload_id`/parts in the prepare answer → a failed send with the reason; the daily limit
  → failed at once, no retry.
- Send-only open (`indus send`): token only; no gateway socket.

Our design differences / decisions:
- Our names; one WebSocket protocol object over an injectable opener (no aiohttp).
- Prefixed chat ids (`group:`, `channel:`, `dm:`; C2C bare) instead of an in-memory chat-kind map
  that forgets after a restart (a cron target or a reply after restart would go to the wrong
  endpoint).
- `msg_id` + growing `msg_seq` on every piece of a reply (the reference sends `msg_id` only on the
  first piece, so the rest count as active messages).
- Approval keyboard carries our short callback data (request id + choice) and offers all four
  choices; the core decides who may answer (a stranger's press changes nothing).
- Markdown off by default (plain text works for every bot; Markdown needs QQ's approval).
- Also handle `AT_MESSAGE_CREATE` (public guild @-messages, the event the guild intent delivers).
- Missing heartbeat ACK detection (zombie socket) added.
- Not built: speech-to-text of voice through ffmpeg/pilk or a separate STT provider (the core's
  intake owns transcription; QQ's own transcript is passed on), quoted-message context, the
  update-prompt keyboard, QR onboarding (P16-31), webhook (HTTP callback) delivery — the row asks
  for the WebSocket gateway only.

Tests we will write:
- gateway: identify after Hello, heartbeat cadence with the fake clock, resume after a drop keeps the
  sequence (Verify), op 7 / op 9 true/false, close codes 4004/4006/4008/fatal, 100 failed reconnects
  → fatal, quick disconnects → fatal, missing ACK → reconnect.
- upload: part retry count exact (Verify), daily limit not retried (Verify), part-finish retry until
  the timeout, concurrency cap, short read, prepare parsing.
- adapter: chat kinds and ids, dedupe, mention strip, attachments and the host guard, interactions
  acked, msg_id/msg_seq, keyboard approvals and guild text fallback, typing rules, token refresh on
  401, send-only, create_adapter.
- contract: the shared suite (`qqbot`).

## Decisions for the lead
- Registry row (P15 core, not edited): `qqbot`, label "QQ Bot", factory
  `indus.gateway.adapters.qqbot:create_adapter`, probe `modules_installed("httpx_ws")`, extra
  `qqbot`, `required_env=("QQ_APP_ID", "QQ_CLIENT_SECRET")`, cap 4,000.
  `BUILTIN_CREDENTIALS["qqbot"] = ("QQ_APP_ID", "QQ_CLIENT_SECRET")`.
- Passive-reply window taken as 300 s for every chat kind (QQ's windows differ per kind).
- Markdown default off.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] token fetch with app id + secret, refresh a minute early, one fetch at a time (test:
  `test_upload.py` rigs, `test_adapter.py::test_a_401_refreshes_the_token_once`)
- [x] Hello → Identify (intents C2C/group, guild @, guild DM, interactions; shard [0, 1]) / Resume
  with session id and last seq (Verify; test: `test_adapter.py::test_resume_after_a_drop_keeps_the_sequence`)
- [x] heartbeat with the last seq at 80 % of the interval (test:
  `::test_heartbeats_follow_the_hello_interval_and_a_missing_ack_replaces_the_socket`)
- [x] op 7 closes and resumes; op 9 true resumes, false identifies afresh (test: `::test_server_requests_op7_and_op9`)
- [x] close codes 4004 (token), 4006/4007/49xx (session), 4008 (60 s), fatal set; backoff 2/5/10/30/60;
  100 attempts; three quick closes (tests: `::test_close_codes`, `::test_a_hundred_failed_attempts_are_fatal`,
  `::test_three_quick_closes_in_a_row_are_fatal`)
- [x] C2C / group / guild channel / guild DM handlers, @-mention stripped, dedupe 1,000 × 300 s, empty
  dropped (test: `::test_chat_kinds_ids_mentions_and_dedupe`)
- [x] attachments: `//` URLs, voice → WAV copy + QQ transcript, download with the bot token from QQ
  hosts only (test: `::test_attachments_voice_transcripts_and_the_fetcher`)
- [x] interactions acknowledged (`PUT /interactions/{id}`, code 0) then handed on (test:
  `::test_button_presses_are_acknowledged_and_handed_on`)
- [x] text / Markdown bodies, `msg_id` + `msg_seq`, guild body shape (tests:
  `::test_passive_replies_name_the_message_with_a_growing_seq`, `::test_guild_bodies_markdown_and_errors`)
- [x] typing: C2C only, input_notify 60 s, every 50 s (test:
  `::test_typing_only_in_c2c_with_a_fresh_message_and_at_most_every_50_seconds`)
- [x] keyboards for approvals in C2C/groups, text in guild chats (test:
  `::test_keyboards_for_approvals_and_choices_text_in_guild_chats`)
- [x] chunked upload: prepare hashes (md5, sha1, md5_10m), parts with concurrency ≤ 10, PUT 300 s with
  exactly 2 retries (Verify), part-finish retry each 1 s to retry_timeout (default 120, max 600),
  complete with 2 retries, daily limit 40093002 never retried (Verify) (tests: `test_upload.py`,
  `test_adapter.py::test_the_daily_upload_limit_is_not_retried`, `::test_media_upload_and_send`)
- [x] R06.3: a long QQ rate limit is handed over, not slept (test:
  `::test_a_long_qq_rate_limit_is_handed_over_without_sleeping`)
- [x] gap found at compare: heartbeat share was 90 % — set to 80 % like the reference's margin.
- Deliberate differences: the reference retries a failed text send three times inline (1 s, 2 s) and
  waits up to 15 s for a reconnect before sending; ours leaves retries to the core's retry policy
  (R06.4: no blind retry after a read timeout) and sends over REST whether or not the socket is up.
  The reference authorises button presses inside the adapter against a session key carried in the
  button data; ours carries only our short callback data and the core's approval rules decide who
  may answer. The reference keeps a chat-kind map in memory (C2C assumed after a restart); ours puts
  the kind in the chat id. Upload completion retries only server errors (a 4xx is final).
- Not taken: URL uploads (QQ fetching a remote URL), voice transcription through ffmpeg/pilk or an
  STT provider, quoted-message context (`message_type` 103), the update-prompt keyboard and
  `.update_response` file, QR onboarding, the out-of-process standalone sender (`indus send` uses
  the adapter's send-only open), webhook mode.

## Status

Done (2026-10-20). Shared files: `pyproject.toml` (extra `qqbot`), `THIRD_PARTY_NOTICES.md`
(httpx-ws / wsproto rows), `tests/contract/gateway/conftest.py` (one import),
`src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py`
(`platforms.qqbot`), `docs/internals/traps-ledger.md` (R06.3 test reference). Registry row and
`BUILTIN_CREDENTIALS` row left to the lead (above).

## Landing (integrator, 2026-10-20)

`test_parts_run_in_parallel_only_as_far_as_allowed` failed under load: the fake's part PUT held for one
loop iteration, so overlap depended on when the worker-thread file reads finished. The fake now has a
count-based `put_gate` (PUTs wait until that many run at once) and the test asserts exactly 3. Lock
key `QQ_APP_ID` (D-16.454).
