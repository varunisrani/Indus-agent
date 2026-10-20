# Task: WeCom callback adapter (P16-18) Phase: 16 Owner: wave-25 builder E8

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/wecom/callback_adapter.py`
(all: apps, listener, GET verification, POST decrypt, event building, dedupe, token cache and refresh on
40001/42001, text send), `wecom/wecom_crypto.py` (crypto steps), `wecom/gateway_env.py` (callback rows);
test names in `tests/gateway/test_wecom_callback.py`. Evidence: none.
Interop (vendor): WeCom callback crypto document (path 90968): EncodingAESKey 43 characters, AESKey =
Base64_Decode(EncodingAESKey + "="), 32 bytes; IV = its first 16 bytes; AES-CBC; PKCS#7 padding to a
multiple of 32 bytes; plaintext = 16 random bytes + 4-byte big-endian length + msg + receiveid (the
corp id for a corp app); signature = lower-case hex SHA1 of the sorted, concatenated token,
timestamp, nonce and encrypted text; GET `msg_signature`, `timestamp`, `nonce`, `echostr` → answer
the decrypted text exactly; POST XML `<xml><ToUserName/><Encrypt/><AgentID/></xml>`. Received
message types (path 90239): text `Content`, image `PicUrl` + `MediaId`, voice `MediaId` + `Format`,
video `MediaId` + `ThumbMediaId`, location, link, plus `event` (`enter_agent`, `subscribe`); common
`ToUserName`, `FromUserName`, `CreateTime`, `MsgType`, `MsgId`, `AgentID`. REST: `GET
/cgi-bin/gettoken?corpid&corpsecret` → `access_token`, `expires_in`; `POST
/cgi-bin/message/send?access_token` `{touser, msgtype: text, agentid, text.content}` (content ≤2,048
bytes); `GET /cgi-bin/media/get?access_token&media_id`.

What it does (own words): a self-built WeCom app sends every message a person writes to it as an
encrypted XML POST to our callback URL. The adapter listens through the P16-01 kit (loopback by
default, body ≤64 KiB), first checks the SHA1 signature over the raw query values and the encrypted
text **in constant time**, then decrypts with AES-256-CBC (`cryptography`, never our own AES), checks
the PKCS#7 padding, the declared length and that the trailing receive id is our corp id, and only
then parses the inner XML. XML is parsed by a parser that refuses any DTD, entity declaration or
external reference (XXE, billion laughs). WeCom's URL check (GET) gets the decrypted `echostr` back.
Replies go out later through `message/send` with an access token cached until 5 minutes before
expiry (refreshed once when WeCom calls it invalid).

Inputs → outputs: `AdapterConfig` (`WECOM_CALLBACK_CORP_ID`, `WECOM_CALLBACK_CORP_SECRET`,
`WECOM_CALLBACK_TOKEN`, `WECOM_CALLBACK_ENCODING_AES_KEY`, `WECOM_CALLBACK_AGENT_ID` or
`platforms.wecom_callback.agent_id`; listener keys `webhook_*`) → listener; signed callbacks →
`InboundEvent`s (DM chat id = the user id); sends → `message/send`; media refs
(`wecom-media://<MediaId>`) → bytes through `media/get`.

Files: `src/indus/gateway/adapters/wecom/callback.py`, `.../wecom/crypto.py`,
`tests/unit/gateway/adapters/wecom/test_callback.py`, `tests/contract/gateway/wecom_callback_case.py`,
the callback wire fake in `tests/fakes/platform/wecom_wire.py`,
`tests/fixtures/gateway/crypto_vectors/wecom_callback.json` + `make_wecom_vectors.sh` (vectors made
with openssl and coreutils from the vendor spec, not with our code).

Edge cases / failure modes:
- bad or missing signature → 401 before any decryption; stale timestamp (±300 s) → 401;
- base64 garbage, a cipher text not a multiple of 16, bad padding (0 or >32, bytes not all equal),
  a declared length past the end, a wrong receive id → 400 (never a 500 with detail);
- `<!DOCTYPE`, entities, external references, oversized bodies → refused; XML without `Encrypt` → 400;
- duplicate `MsgId` (WeCom retries a slow callback) → 200 "success", no second event;
- lifecycle events (`enter_agent`, `subscribe`) acknowledged and dropped; location and link become
  text; image / voice / video become attachments;
- `message/send` answering 40014 / 42001 / 40001 → token dropped and the send retried once;
  other codes classified (45009 rate limit, 81013 / 60111 / 40003 user not found → chat not found,
  60011 / 48002 forbidden, 45002 too long, 40058 / 44004 bad format);
- the access token and corp secret travel only in the transport's copy of the URL (never in httpx's
  log line, `response.request` or error texts).

Our design differences / decisions:
- our names (`CallbackCrypto`, `WeComCallbackAdapter`, `parse_xml`, `CryptoError`);
- one app per profile (the reference takes a list of apps): a second app is a second profile;
- signature compare in constant time, replay window on the timestamp (the reference has neither);
- XML parsed by our hardened stdlib expat wrapper (DTD/entity/external-reference handlers refuse),
  not defusedxml — see "Decisions for the lead";
- cap 2,048 UTF-8 bytes per message (the vendor truncates beyond it), split by bytes;
- reply `text` messages (plain formatter); no outbound media in 1.0 (refused cleanly).

Tests we will write: known-answer vectors from the fixture (signature, decrypt, echostr); the
contract over the callback wire fake; XXE / billion-laughs / DOCTYPE payloads refused before any
event; signature checked before decrypt (decrypt spied, never called on a bad signature) and through
the constant-time helper; padding and receive-id failures; replay window; duplicate MsgId; token
refresh on 40001; logs never show the token or secret; vectors script reproducible.

## Decisions for the lead
- The row says "defusedxml only". defusedxml is not in the lock and the gate runs without extras, so
  the XXE tests would skip there. We parse with the standard library's expat configured the same way
  defusedxml configures it (a DOCTYPE, an entity declaration or an external reference raises, no
  parameter-entity parsing) in `wecom/crypto.py::parse_xml`, so the security tests run in every gate.
  To use defusedxml instead, add `defusedxml==0.7.1` (PSF-2.0) to the `wecom` extra and swap the body
  of `parse_xml`.
- `cryptography` comes in through keyring → SecretStorage on Linux (Apache-2.0 OR BSD-3-Clause); it is
  not a direct dependency, so the `wecom` extra pins the locked `cryptography==50.0.2` and the adapter
  imports it lazily (missing → fatal `missing_dependency` with the install hint).
- Default port 8645 and path `/wecom/callback` (the reference's); BlueBubbles also defaults to 8645
  (P16-24): the doctor's port-conflict check (P16-36) reports it when both run.
- Registry row (P15 core): `replace(_chat_entry("wecom_callback", "WeCom (callback app)",
  sdk=("cryptography",), extra_name="wecom", required_env=("WECOM_CALLBACK_CORP_ID",
  "WECOM_CALLBACK_CORP_SECRET", "WECOM_CALLBACK_AGENT_ID", "WECOM_CALLBACK_TOKEN",
  "WECOM_CALLBACK_ENCODING_AES_KEY"), max_len=2048, hint=...), factory=_lazy_factory(
  "indus.gateway.adapters.wecom:create_callback_adapter"), push_capable stays True)`;
  `BUILTIN_CREDENTIALS["wecom_callback"] = ("WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET",
  "WECOM_CALLBACK_TOKEN", "WECOM_CALLBACK_ENCODING_AES_KEY")` (agent id read by the adapter).

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] signature over sorted token/timestamp/nonce/encrypt, checked in constant time before any
  decryption; decrypt layout; padding and receive id checked (tests:
  `wecom/test_callback.py::test_signatures_match_the_vectors`,
  `::test_decryption_matches_the_vectors`, `::test_encryption_matches_openssl_given_the_same_random_prefix`,
  `::test_the_signature_is_checked_in_constant_time_before_decrypting`, `::test_bad_inputs_fail_closed`)
- [x] known-answer vectors made by `make_wecom_vectors.sh` with openssl (`::test_the_vectors_file_is_what_the_script_makes`)
- [x] GET echo of the decrypted echostr (`::test_url_verification_echoes_the_decrypted_echostr`); POST
  ack "success"
- [x] XXE / billion laughs / DOCTYPE refused, outer and inner (`::test_the_parser_refuses_dtds_entities_and_references`,
  `::test_xxe_payloads_are_refused_before_any_event`)
- [x] dedupe by MsgId (else user + CreateTime), stale timestamps, wrong corp
  (`::test_stale_timestamps_wrong_corps_and_repeats`); lifecycle events dropped
  (`::test_message_types_and_lifecycle_events`)
- [x] token cache with margin, refresh once on 40001/42001/40014; 2,048-byte text cap split by bytes
  (`::test_replies_are_split_by_bytes_and_tokens_are_refreshed_once`)
- [x] body cap 64 KiB (`::test_oversized_bodies_are_refused_unread`); token and secret never logged
  (`::test_the_token_and_secret_never_reach_logs`)
- [x] shared contract over the callback wire (`tests/contract/gateway/wecom_callback_case.py`)
- Deliberate differences: one app per profile; loopback bind by default (the kit); constant-time
  signature compare and a ±300 s replay window (the reference has neither); our hardened expat parser
  instead of defusedxml (Decisions); image, voice, video, location and link messages become events
  (the reference keeps text only); events other than the person's messages start no turn (the
  reference turns some into `/start`); errors classified by WeCom code.
- Not taken: several apps in one adapter, outbound media for the callback app, passive (encrypted)
  replies in the HTTP answer.

## Status

Done (2026-10-20). New files: `wecom/callback.py`, `wecom/crypto.py`, tests, the callback wire fake
(in `tests/fakes/platform/wecom_wire.py`), contract case, `tests/fixtures/gateway/crypto_vectors/`
(`make_wecom_vectors.sh`, `wecom_callback.json`). Shared: config schema row
`platforms.wecom_callback` + test, contract `conftest.py`. The callback contract case registers only
when `cryptography` is importable (always on Linux, where keyring brings it).
