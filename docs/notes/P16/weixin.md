# Task: Weixin adapter, iLink Bot (P16-19) Phase: 16 Owner: wave-27 builder E12

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/weixin/adapter.py`
(all: constants and endpoints, headers, AES helpers and key formats, context-token store, typing
tickets, long-poll loop and its error handling, inbound parsing and dedupe, text splitting, send
with stale-session and rate-limit handling, media upload and download, QR login, registration),
`weixin/gateway_env.py`, `weixin/plugin.yaml`; test names only in `tests/gateway/test_weixin_*.py`.
Evidence: none.
Interop (vendor): Tencent iLink Bot HTTP API as published with Tencent's Weixin channel package for
bot frameworks (no public reference document; names below as the vendor uses them, not verified
against a live account here): base `https://ilinkai.weixin.qq.com`; every call is a JSON `POST
ilink/bot/<name>` with `base_info.channel_version`, headers `AuthorizationType: ilink_bot_token`,
`Authorization: Bearer <token>`, `X-WECHAT-UIN` (base64 of a random uint32 in decimal),
`iLink-App-Id`, `iLink-App-ClientVersion`; `getupdates {get_updates_buf}` → `{ret, errcode, errmsg,
msgs[], get_updates_buf, longpolling_timeout_ms}`; `sendmessage {msg: {from_user_id, to_user_id,
client_id, message_type: 2, message_state: 2, item_list, context_token}}`; `getconfig
{ilink_user_id, context_token}` → `typing_ticket`; `sendtyping {ilink_user_id, typing_ticket,
status 1|2}`; `getuploadurl {filekey, media_type 1..4, to_user_id, rawsize, rawfilemd5, filesize,
no_need_thumb, aeskey}` → `upload_param` / `upload_full_url`; CDN `POST <cdn>/upload?
encrypted_query_param&filekey` (answer header `x-encrypted-param`), `GET <cdn>/download?
encrypted_query_param`; items `type` 1 text, 2 image, 3 voice, 4 file, 5 video; media blocks
`{encrypt_query_param, aes_key, encrypt_type, full_url}`; AES-128-ECB with PKCS#7 for media, the
key as base64 of 16 raw bytes or of 32 hex characters (outbound: base64 of the hex). `ret`/`errcode`
`-14` session expired, `-2` frequency limit (with `errmsg` "unknown error"/"prepare failed" a
session that is not ready).

What it does (own words): Indus Agent answers a personal WeChat account through Tencent's iLink bot
gateway. A long poll (`getupdates`, server-suggested wait, ~35 s) brings the messages; the cursor
(`get_updates_buf`) is saved after the messages it covered were handed on, so a restart neither
loses nor repeats them. Each message carries a `context_token` for its conversation: the adapter
keeps the newest one per chat (on disk, 0600, under the profile home) and must echo it on every
reply — a chat without one is refused with a clear error (the person has to write first). Text
replies are cut at 2,000 characters; long lines are wrapped for copying and blank-line runs
collapsed (code blocks untouched). Media go through Tencent's CDN encrypted with AES-128-ECB, the
vendor's scheme (ECB is mandated by the vendor protocol, not our choice): outbound files get a fresh
random key, are encrypted, uploaded, then sent as an item; inbound media are fetched for the intake
from the CDN (only Tencent hosts, https) and decrypted. Messages are deduplicated for 300 s (by id
and by sender + text). Group messages are off by default and, when on, need a mention. The bot
token and context tokens never appear in logs, errors or reprs.

Inputs → outputs: `AdapterConfig` (`WEIXIN_TOKEN`, `WEIXIN_ACCOUNT_ID` from the secret store/env;
`WEIXIN_BASE_URL` / `platforms.weixin.base_url`, `WEIXIN_CDN_BASE_URL` / `cdn_base_url`,
`group_policy`, `group_allow_from`, `mention_names`, `max_media_bytes`) → connected adapter;
`getupdates` JSON → `InboundEvent`s; send/typing/media calls → iLink requests → `SendResult`s;
`weixin-media://` refs → decrypted bytes via the registered fetcher.

Files: `src/indus/gateway/adapters/weixin/__init__.py` (factory), `adapter.py` (adapter),
`api.py` (our iLink client over httpx), `media.py` (AES-128-ECB via `cryptography`, key formats, CDN
host check), `tests/fakes/platform/weixin_wire.py`, `tests/contract/gateway/weixin_case.py`,
`tests/unit/gateway/adapters/weixin/test_adapter.py`, `tests/fixtures/gateway/crypto_vectors/
weixin_media.json` + `make_weixin_vectors.sh` (openssl, not our code); shared: contract
`conftest.py`, config schema row, `pyproject.toml` extra, `THIRD_PARTY_NOTICES.md`.

Edge cases / failure modes:
- No stored context token for the chat → refused (`no conversation token`), final, never sent
  without one; `-14` on a send drops the stored token and fails final with the same advice.
- `-2` frequency limit → rate limited (3 s) and a 30 s circuit that answers later sends at once;
  `-2` with "unknown error"/"prepare failed" → session not ready (final, not a rate limit).
- Poll: a client-side read timeout is an empty answer; `-14` pauses 10 minutes; 3 failures in a row
  back off 30 s (else 2 s); 401/403 → fatal (`weixin_auth`).
- Our own messages (sender = the bot account) and duplicates are dropped; empty messages dropped.
- Media: URL hosts outside Tencent's CDN list or non-https refused; declared/streamed size capped;
  a bad key format refused; wrong padding returns the raw blocks (as the vendor client).
- Send-only (`indus send`): no long poll, no cursor change; stored context tokens are read.

Our design differences / decisions:
- Own iLink client over httpx (no aiohttp): one client, token only in the iLink requests' headers,
  never on CDN requests, redirects off.
- Tokenless sends are not made (the reference re-sends without the token after a stale session and
  sends tokenless when none is stored); the phase asks for a clear refusal.
- Context tokens keyed by chat (the reply target), not by sender.
- Authorization: the core chain (allowlists, pairing) decides DMs (`owns_access_policy` false);
  the adapter only applies `group_policy` (`disabled` default, `open`, `allowlist`) and the mention
  flag; the reference has its own DM policy too.
- No text-burst batching in the adapter (the core's busy/queue rules handle bursts); no QR login
  (P16-31 setup helper); no remote-URL image download in the adapter (the media policy sends local
  files only); voice is always fetched as audio for the core's STT (the reference does the same).

Tests we will write:
- contract suite over the wire fake; reply without a stored token refused with a clear error;
- media round trip with test vectors (openssl-made known answers, our encrypt → fake CDN → decrypt);
- cursor saved after dispatch, not when unchanged; restart resumes from it;
- dedupe by id and by content within 300 s; own messages dropped; group policy and mention;
- `-2` rate limit and circuit, stale session, `-14` on send and on poll, 401 fatal;
- token and context tokens never in logs/errors/repr; send-only does no poll.

## Decisions for the lead
- Registry row (P15 core, not edited): `_chat_entry("weixin", "Weixin / WeChat", sdk=("cryptography",),
  extra_name="weixin", required_env=("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID"), max_len=2000,
  hint="You are talking through Weixin (WeChat). Markdown is shown as typed, so keep formatting
  light and replies short.")` with `lock_env=("WEIXIN_ACCOUNT_ID",)` (a new token for the same
  bot is the same bot); `BUILTIN_CREDENTIALS["weixin"] = ("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID")`;
  `BUILTIN_NAMES` gains `weixin` after `wecom_callback`; `cron.targets.KNOWN_PLATFORMS` gains
  `weixin`.
- Extra `weixin = ["cryptography==50.0.2"]` (already locked; AES only, imported lazily).
- Context-token file `<home>/platforms/weixin/<account>/context_tokens.json` and cursor
  `cursor.json` (0600, directory 0700).

Compare checklist (filled after the rewrite; reopened `plugins/platforms/weixin/adapter.py`,
`gateway_env.py`, `plugin.yaml`):
- [x] Headers, `base_info`, endpoints and item shapes (`test_adapter.py::test_the_fake_serves_every_client_route`,
  the wire fake checks the headers and `base_info` on every call).
- [x] Context token stored per conversation and echoed; none → refused, never sent tokenless
  (`::test_a_reply_without_a_stored_context_token_is_refused_clearly`, `::test_an_expired_session_on_send_drops_the_token`).
- [x] AES-128-ECB with PKCS#7, key formats, hex-in-base64 outbound key, md5 and padded size in
  `getuploadurl`, POST to the CDN, `x-encrypted-param` (`::test_media_crypto_matches_the_known_answer_vectors`,
  `::test_media_round_trip_through_the_cdn`).
- [x] Poll: client timeout is empty, `-14` pauses 10 minutes, three failures back off 30 s, cursor saved
  only when it moved and after dispatch (`::test_poll_errors_back_off_pause_on_an_expired_session_and_stop_on_a_refused_token`,
  `::test_the_cursor_is_saved_after_dispatch_and_resumed_after_a_restart`).
- [x] `-2` frequency limit vs. "unknown error"/"prepare failed" (not a rate limit), 30 s circuit
  (`::test_a_frequency_limit_pauses_sends_and_an_unready_session_is_final`).
- [x] Dedupe by id (300 s) and by sender + text; own messages dropped (`::test_duplicates_own_messages_and_empty_messages_are_dropped`).
- [x] Voice always fetched as audio; transcript only without audio; quoted text kept
  (`::test_voice_quotes_and_bot_messages`).
- [x] Typing ticket from `getconfig`, cached 10 minutes (`::test_typing_uses_a_cached_ticket`).
- [x] Text normalisation and copy-friendly wrapping; 2,000-character pieces
  (`::test_long_replies_split_at_2000_characters_and_lines_wrap_for_copying`, `::test_prepare_text_keeps_code_and_tables`).
- [x] Gap found: pieces of one reply left back to back; the reference waits 1.5 s between them —
  `slot_spacing` 1.5 s.
- [x] Gap found: the reference warns that iLink bots rarely get group events — logged at connect
  when groups are on.
- Deliberate differences: no tokenless sends (refused instead); tokens keyed by chat; the core's
  authorization chain for DMs; content dedupe window 60 s (reference 300 s: a person repeating "ok"
  within five minutes would be dropped); inbound `full_url` must be https on a Tencent host (the
  reference allowed http); a `message_id` in the `sendmessage` answer is used when present, else our
  client id; one message per piece (no splitting of short "chatty" multi-line replies into bubbles);
  no voice-as-attachment caption; the guess that a message to another `to_user_id` is a group is
  not taken (only `room_id` makes a group).
- Not taken: QR login (P16-31), text-burst batching (core), remote-URL image download, the
  out-of-process live-adapter reuse.

## Status

Done (2026-10-20). New: `src/indus/gateway/adapters/weixin/{__init__,adapter,api,media}.py`,
`tests/fakes/platform/weixin_wire.py`, `tests/contract/gateway/weixin_case.py`,
`tests/unit/gateway/adapters/weixin/{__init__,test_adapter}.py`,
`tests/fixtures/gateway/crypto_vectors/{make_weixin_vectors.sh,weixin_media.json}`. Shared:
`tests/contract/gateway/conftest.py` (case import), `src/indus/foundation/config/schema.py` +
`tests/unit/foundation/config/test_schema.py` (`platforms.weixin`), `pyproject.toml` (`weixin` extra),
`THIRD_PARTY_NOTICES.md` (cryptography/cffi rows), `docs/internals/acceptance-map.md` (ACC-K11-4).
Registry row and credential table are the lead's (proposed above).

Landed at integration (wave 27): registry row, credentials, lock key, target rule, setup step and an `indus send` case (D-16.528 … D-16.530, D-16.537, D-16.538). Integration fix: a configured CDN base must be https on a Tencent media host (refused at connect, `config`); the cursor test waits on the io pool's fsync'd writes instead of a fixed real-time pause.
