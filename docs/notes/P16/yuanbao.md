# Task: Yuanbao adapter (P16-21) Phase: 16 Owner: wave-27 builder E14

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/yuanbao/adapter.py`
(module constants, the sign-token manager, the inbound middleware chain — decode, fields, dedupe,
skip-self, routing, access guard, content extraction, group mention guard, dispatch — the
connection manager: open, dial, auth-bind, heartbeat, receive loop, frame handling, reconnect,
cleanup; media send handlers, reply heartbeat manager, message sender, the adapter class),
`yuanbao/yuanbao_proto.py` (all: the ConnMsg head, biz wrapping, field numbers of every message),
`yuanbao/yuanbao_media.py` (all: upload info, COS signing and PUT, TIM image/file bodies, image
size sniffing, download caps), `yuanbao/register.py`, `yuanbao/gateway_env.py`,
`yuanbao/plugin.yaml`, `yuanbao/__init__.py`; test names only in `tests/gateway/test_yuanbao_*.py`
and `tests/gateway/platforms/test_yuanbao_*.py`. Evidence: none.

What it does (own words): connects Indus Agent to Tencent Yuanbao bots. The bot's App ID and App
Secret buy a sign token over HTTPS (`POST <api>/api/v5/robotLogic/sign-token`, JSON
`{app_key, nonce, signature, timestamp}`; the signature is HMAC-SHA256 keyed by the secret over
nonce + timestamp + app id + secret, hex; the timestamp is Beijing time in ISO form without
milliseconds). The answer gives the token, the bot's account id, a source tag and the token's
lifetime (seconds, default one hour); code 10099 means "try again" (three more tries a second
apart), any other non-zero code is a refusal. The bot then holds one WebSocket
(`wss://bot-wss.yuanbao.tencent.com/wss/connection` by default) whose binary frames are protobuf
`ConnMsg {Head head = 1; bytes data = 2}` with `Head {cmd_type 1, cmd 2, seq_no 3, msg_id 4,
module 5, need_ack 6, status 10}`; `cmd_type` 0 request, 1 response, 2 push, 3 push ack. The
first frame is `auth-bind` (module `conn_access`) carrying the token, the bot id and a device
description; the answer's `AuthBindRsp {code 1, message 2, connect_id 3}` must have code 0 and a
connect id. Every 30 s a `ping` request goes out; a `ping` response must come back within 10 s;
two misses in a row mean the socket is dead and it is replaced. A drop reconnects after 1, 2, 4 …
60 s with a fresh token; after 100 failed reconnects in a row the platform is fatal. Close codes
4012, 4013, 4014, 4018, 4019 and 4021 are permanent (fatal at once).

Inbound: pushes (`cmd_type` 2) are acknowledged when `need_ack` is set and their data is an
`InboundMessagePush` (protobuf; a JSON form is also accepted): sender account (2), sender
nickname (4), group code (6), group name (7), message id (12, else the message key 11), body
(13: repeated `{msg_type, msg_content}` elements), quote data (14), the bot owner (16), recall
callbacks (1). Element kinds: `TIMTextElem` (text), `TIMImageElem` (image infos with URLs),
`TIMSoundElem`, `TIMVideoFileElem`, `TIMFileElem` (url, file name), `TIMFaceElem` (a sticker,
JSON data with a name), `TIMCustomElem` (JSON data; `elem_type` 1002 is an @mention with
`user_id` and `text`). Group chats are `group:<code>`, DMs `direct:<account>`.

Outbound: text goes as `send_c2c_message` (`SendC2CMessageReq`: msg id 1, to 2, from 3, random
4, body 5, group code 6 — a DM that started in a group —, seq 7) or `send_group_message`
(`SendGroupMessageReq`: msg id 1, group code 2, from 3, to 4, random 5, body 6, quoted message 7,
seq 8), both with module `yuanbao_openclaw_proxy`; the response with the same msg id (or its
absence after 30 s) decides the outcome. Markdown renders; 4,000 characters per message. While a
turn runs the bot sends "reply in progress" heartbeats (`send_private_heartbeat` /
`send_group_heartbeat`, value 1 RUNNING) every 2 s, stops after 30 s without renewal, and a
FINISH (value 2) after the reply is delivered. Media: `POST <api>/api/resource/genUploadInfo`
gives temporary COS credentials; the file is PUT to `<bucket>.cos.accelerate.myqcloud.com` with a
COS `q-sign-algorithm=sha1` Authorization header and the security token; the message then carries
a `TIMImageElem` (md5 uuid, format, size, width, height, url) or a `TIMFileElem` (uuid, name,
size, url). Inbound media URLs that name a `resourceId` are exchanged for a direct URL through
`GET <api>/api/resource/v1/download` (token headers) before downloading. Group queries (used by
the P16-22 tools): `query_group_info` (`{group_code}` → `{code, message, {name, owner id, owner
nickname, size}}`) and `get_group_member_list` (`{group_code, offset, limit}` → members `{user_id,
nickname, role, join_time, name_card}`, next offset, complete flag).

Inputs → outputs: `AdapterConfig` (`YUANBAO_APP_ID`, `YUANBAO_APP_SECRET`; optional
`YUANBAO_BOT_ID`, `YUANBAO_WS_URL`, `YUANBAO_API_DOMAIN`, `YUANBAO_ROUTE_ENV` from the secret
store / env, or `platforms.yuanbao.{bot_id, ws_url, api_domain, route_env, dm_policy,
group_policy, allow_from, group_allowed_chats, media_hosts, max_media_bytes, instance_id}`) → a
connected adapter; pushes → `InboundEvent`s; sends, media, typing → frames → `SendResult`s.

Files: `src/indus/gateway/adapters/yuanbao/__init__.py`, `.../yuanbao/adapter.py`,
`.../yuanbao/frames.py` (our protobuf wire codec), `.../yuanbao/client.py` (sign token, REST,
COS upload, the socket opener), `.../yuanbao/stickers.py` (the sticker catalogue, shared with
P16-22), `tests/fakes/platform/yuanbao_wire.py`, `tests/contract/gateway/yuanbao_case.py`,
`tests/unit/gateway/adapters/yuanbao/test_adapter.py`, `.../yuanbao/test_frames.py`,
`.../yuanbao/_stubs_e14.py` (stand-in registry/config rows); shared: `pyproject.toml` (extra
`yuanbao`), `THIRD_PARTY_NOTICES.md` (httpx-ws / wsproto rows), `tests/contract/gateway/conftest.py`
(one import line), `foundation/config/schema.py` + its test (row `platforms.yuanbao`),
`gateway/adapters/targets.py` (rule), `cli/wizards/gateway_setup.py` (steps row).

Vendor API: Yuanbao bot gateway — HTTP `POST /api/v5/robotLogic/sign-token`,
`POST /api/resource/genUploadInfo`, `GET /api/resource/v1/download`; COS `PUT` (Tencent COS
signature version `sha1`, `x-cos-security-token`); WebSocket commands `auth-bind`, `ping`,
`kickout`, push ack, `send_c2c_message`, `send_group_message`, `send_private_heartbeat`,
`send_group_heartbeat`, `query_group_info`, `get_group_member_list`. No public API version or SDK
exists; the field numbers above are the interop facts. WebSocket client: `httpx-ws` 0.9.0 (MIT,
needs `wsproto` 1.3.2 MIT; both already locked), new extra `yuanbao`, imported lazily; HTTP on
httpx; HMAC through the stdlib `hmac` module; protobuf framing by our own small codec (wire format
only, no crypto).

Capability flags (research 06 Q4): `dm_is_private` true; `owns_access_policy` true (own
`dm_policy` / `group_policy`: open, allowlist, pairing, disabled; DM allowlist in `allow_from`,
group allowlist in `group_allowed_chats`); `auth_upstream` false; `supports_edit` false;
`supports_delete` false; `supports_buttons` false (approvals as text with `/approve`);
`push_capable` true; `splits_natively` true; cap 4,000 code points; markup `markdown`.

Edge cases / failure modes:
- Missing App ID / Secret → connect refused, not retryable (`config`). A sign-token refusal
  (non-zero code other than 10099, HTTP 401/403) → not retryable (`yuanbao_auth`); network and 5xx
  → retryable. While running, a refusal on reconnect reports fatal.
- `ws_url` / `api_domain` overrides must be `wss://` / `https://` (plain only to a loopback
  host): the token never travels in clear to another machine.
- Auth-bind answered with a non-zero code or without a connect id, or nothing within 10 s →
  the attempt fails (retryable at connect; counts as a failed reconnect later).
- Frames that do not decode are dropped; responses nobody waits for are ignored; a `kickout` push
  closes the socket (another connection took the bot) and the normal reconnect follows.
- Send to a closed socket → never left (retryable); no answer within 30 s → maybe delivered,
  never retried (R06.4); a refusal (head status or answer code ≠ 0) carries Yuanbao's message;
  a stated wait in it ("retry after N") becomes `retry_after` and is handed to the ledger, never
  slept inline (R06.3).
- Repeats (message id, 300 s) and the bot's own messages dropped; recall callbacks dropped
  (nothing to retract); group messages from groups outside an `allowlist` group policy or under a
  `disabled` policy dropped.
- Inbound media only from https hosts on the media allowlist (Tencent / Yuanbao / COS hosts plus
  `platforms.yuanbao.media_hosts`), size-capped (50 MiB default), no redirects, the bot token only
  ever sent to the API domain.
- Send-only open (`indus send`): token + socket + auth, pushes neither acknowledged nor read.

Our design differences / decisions:
- Our names and structure: a supervisor task owns the socket (like the WeCom adapter); the token
  cache lives on the adapter, not on a process-wide class; all waits on the injected clock.
- No 1.5 s per-sender merge of pushes into one turn: the core's split-paste batching and busy
  guard already merge rapid messages, and merging would fold separate messages into one.
- Group messages are emitted with `mentioned` (an @mention of the bot) and the core's mention
  gate decides; unaddressed group messages are not written into the transcript as "observed"
  context (no seam for that in the core yet); owner-only slash commands without a mention are
  not special-cased (the core's command authorization applies).
- No slow-response "please wait" notice (the core's heartbeat/notice layer owns that); no auto
  "set home channel" from the first chat (the home channel is set with `/sethome` or the setup
  wizard); no recall redaction of transcripts; no forwarded-chat-history decoding (the custom
  element's summary text is kept); no quoted-message context (its field is not decoded yet).
- A bare chat id in `send` is a DM (`direct:` implied); the target rule accepts only the
  prefixed forms (no "bare number = group" guess).
- Outbound `@nickname` in a group becomes a mention element when the member list is cached.

Tests we will write:
- contract suite (`yuanbao_case.py`): lifecycle, connect failure, sends with ids, split, no whole
  resend, edit refused, typing, approvals as text, inbound shapes, error classification;
- two missed pongs → a new socket (fake clock); 100 failed reconnects → fatal, never a 101st;
  permanent close code → fatal at once;
- sign-token request shape: HMAC over the documented string, Beijing timestamp, retry on 10099,
  refusal not retryable; secrets and the token never in logs, errors, reprs;
- reply heartbeat: RUNNING every 2 s while typing, FINISH after delivery, auto-FINISH after 30 s;
- media: image upload through the fake COS with the signed header, file upload, inbound
  resource exchange + download with caps and host allowlist;
- frames codec round trips and known bytes (`test_frames.py`);
- `indus send` (send-only) opens no inbound consumption.

## Decisions for the lead
- Registry row, credentials, lock key and toolset rows are in the report and in
  `tests/unit/gateway/adapters/yuanbao/_stubs_e14.py` (stand-ins, removed at integration).
- `auth-bind` device info sends instance id `17` (the value the reference client uses; the server
  may key client kinds on it) — overridable with `platforms.yuanbao.instance_id`; check live.
- Error readings are by Yuanbao's message text (no public error-code table exists); check live.
- `indus send` opens a second socket for the same bot; if Yuanbao kicks the gateway's socket when
  that happens, the gateway reconnects (normal path). A live check should confirm; the
  alternative is to refuse send-only and route `indus send` through the running daemon.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/yuanbao/adapter.py`,
`yuanbao_proto.py`, `yuanbao_media.py`, `register.py`):
- [x] sign-token request shape, retry code 10099 (three more tries, 1 s), one-minute cache margin,
  default lifetime (test: `test_adapter.py::test_the_sign_token_is_an_hmac_over_the_documented_string`,
  `::test_a_retry_code_is_tried_again_and_a_refusal_is_final`)
- [x] auth-bind fields (biz id, uid, source, token, device info with instance id, env name),
  connect id, 10 s auth wait (`::test_auth_bind_carries_the_token_and_the_bot_identity`,
  `::test_a_refused_auth_bind_fails_the_connect`; `test_frames.py`)
- [x] heartbeat 30 s / pong 10 s / two misses, backoff 1 → 60 s, 100 attempts, permanent close
  codes (`::test_two_missed_pongs_open_a_new_socket`,
  `::test_the_hundredth_failed_reconnect_is_fatal_and_no_101st_is_made`,
  `::test_a_permanent_close_code_is_fatal_at_once`, `::test_a_kickout_or_a_drop_reconnects_and_resets_the_backoff`)
- [x] inbound: protobuf and JSON pushes, push acks, dedupe, self, recalls, routing, access
  policies, text / media / face / custom elements, full-width slash
  (`::test_inbound_messages_are_normalised`, `::test_own_policies_drop_disabled_and_unlisted_chats`)
- [x] gap found: an answer can arrive as a push carrying the request's msg id — handled, pinned by
  `::test_sends_reach_c2c_and_groups_with_quotes_and_dm_origins` (``push_answers``)
- [x] gap found: placeholder-only messages ("[image]" with no media) are not messages — dropped,
  pinned by `::test_inbound_messages_are_normalised`
- [x] gap found: upload slots are requested as source `web` — fixed, pinned by
  `::test_media_goes_through_a_signed_cos_upload`
- [x] outbound: C2C / group encoding, quote, DM-from-group code, 4,000 cap, mentions from the
  member cache (`::test_long_text_is_cut_at_four_thousand_characters`,
  `::test_outbound_mentions_use_the_cached_member_list`)
- [x] reply heartbeat RUNNING every 2 s, FINISH after delivery, auto-FINISH after 30 s idle
  (`::test_the_reply_heartbeat_runs_while_typing_and_finishes_after_delivery`)
- [x] media: genUploadInfo, COS sha1 signature (known answer), image/file elements, resource
  exchange with a 401 re-sign, download cap (`::test_media_goes_through_a_signed_cos_upload`,
  `::test_the_cos_signature_matches_a_known_answer`,
  `::test_inbound_media_is_exchanged_then_fetched_without_credentials`)
- [x] our own: messages that arrive before the gateway installs its handler are kept for it
  (the daemon connects first, then installs the sink) — `::test_messages_before_the_handler_is_installed_wait_for_it`
- Deliberate differences: no blind per-chunk retries (R06.4: the core's policy decides); no
  `(n/m)` stripping (the core's chunk markers stay); a permanent close code or exhausted
  reconnects report fatal instead of only marking the adapter down; sign-token HTTP 401/403 is a
  final refusal; the token cache is per adapter; inbound media fetched only from an https host
  allowlist (the reference used a private-address guard, which COS hosts trip); no 1.5 s push
  merge; see "Our design differences".
- Not taken: observed group history in the transcript, owner-only slash commands, auto home
  channel, slow-response notice, recall redaction, forwarded chat-history decoding, image sends
  by URL (the core sends files), the process-wide "active adapter" for a standalone sender
  (`indus send` opens its own send-only socket).

## Status

Done (2026-10-20). Shared files: `pyproject.toml` (extra `yuanbao`), `THIRD_PARTY_NOTICES.md` and
`docs/plan/phases/00-layout-and-conventions.md` (httpx-ws / wsproto rows),
`tests/contract/gateway/conftest.py` (one import), `foundation/config/schema.py` + `test_schema.py`
(`platforms.yuanbao`), `gateway/adapters/targets.py` (rule), `cli/wizards/gateway_setup.py`
(steps), `tests/unit/gateway/adapters/test_targets.py` and `tests/unit/cli/wizards/test_gateway_setup.py`
(pending-platform stand-in until the registry rows land), traps / acceptance rows. Registry,
credential, lock-key, cron-target rows are for the lead (report and `_stubs_e14.py`).

Landed at integration (wave 27): the registry row, credentials, lock key and toolset rows are in the core files (D-16.534, D-16.537); `_stubs_e14.py` and the pending-platform stand-ins were removed.
