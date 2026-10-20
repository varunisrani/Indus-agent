# Task: Inbound HTTP listener kit and identity helpers (P16-01) Phase: 16 Owner: wave-23 builder E1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/whatsapp_common.py`
(JID helpers, broadcast filter, allowlist alias matching), `gateway/whatsapp_identity.py` (the
alias expansion it calls: normalising, outbound JID, lid-mapping files, canonical id),
`gateway/authz_mixin.py` lines 95–190 (bech32 decoding, npub allowlist expansion, alias matching
per platform), `gateway/profile_routing.py` lines 20–50 (WhatsApp non-user suffixes),
`plugins/platforms/signal/adapter.py` lines 415–430 and 465–472 (sender number / UUID) and
`gateway/session.py` lines 676–684 (alternate id first for participants). Evidence: none.
Interop: NIP-19 (bech32 `npub`, the spec's example vector), BIP-173 (bech32 checksum, generator,
charset, its valid test strings), WhatsApp JID domains (`s.whatsapp.net`, `lid`, `c.us`, `g.us`,
`broadcast`, `newsletter`) and the Baileys multi-file auth state's `lid-mapping-<id>.json` /
`lid-mapping-<id>_reverse.json` files, Twilio request validation (HMAC-SHA1 over URL + sorted
params, base64), WeCom/WeChat callback signature (SHA1 hex of the sorted token, timestamp, nonce
and message), HTTP `Retry-After` (RFC 9110: seconds or an HTTP date).

What it does (own words): later adapters that receive vendor callbacks over HTTP (WhatsApp
Cloud, SMS, Teams, LINE, BlueBubbles, WeCom callback, Google Chat, Graph, Raft) all need the
same small server: one listener per adapter port, loopback unless the operator explicitly opens
it, a body cap checked before anything is read, signatures compared in constant time, a replay
window, a `/health` answer, and a stop that finishes inside the adapter's 5 s disconnect budget.
The kit gives them that on top of the P15 serving helpers (`gateway.http.serving`: `bind_listener`,
`HttpServer` over uvicorn, `LengthGate`, `SecurityHeaders`, `read_body`), so no adapter writes its
own server. It also gives outbound vendor calls one way to turn an httpx failure or an HTTP error
status into a classified `SendResult` (R06.4: a read timeout is never retried, a connect failure
is). The identity helpers turn the platforms' many spellings of one person into one id: WhatsApp
phone JIDs, device-qualified JIDs and LID aliases collapse to one canonical id (groups,
broadcasts and newsletters are never users); Nostr `npub` strings decode to the hex pubkeys
events carry; Signal numbers and UUIDs become `user_id` / `user_id_alt`. The wire fake base lets
each adapter's contract run deliver signed callbacks into its listener in-process (no socket)
and answer its REST calls from the shared fake platform.

Inputs → outputs:
- `ListenerSettings.from_extra(extra, port=, max_body_bytes=, path=)` reads the adapter's
  `platforms.<name>` keys `webhook_host` (127.0.0.1), `webhook_port`, `webhook_path`,
  `webhook_public` (false), `webhook_public_url`, `webhook_max_body_bytes` → settings, or
  `ListenerError` (a config problem).
- `CallbackListener(settings, name=, clock=, scrub=, serve=True)`: `route(path, handler,
  methods=)`; `app` (ASGI, also used in-process by wire fakes); `start()` (binds and serves, or only
  opens the door when `serve=False`); `stop(budget=5.0)`; `background(coro)`; `idle()`; `address`.
  Handlers take a `CallbackRequest` (method, path, raw query, lower-case headers, body bytes,
  client host, receive time; `json()`, `form()`, `query_params()`, `signed_url(public_url)`) and
  return a `Reply` (`Reply.json`, `Reply.text`, `Reply.empty`).
- Answers the kit gives before a handler runs: unknown path 404; wrong method 405 (+ `Allow`);
  not started / stopping 503 (+ `Retry-After`); declared length over the cap 413 and a bad length
  400, before reading; a streamed body over the cap 413 as soon as the cap is passed (the rest is
  never read); a handler error 500 (no detail); `/health` GET/HEAD 200
  `{"status": "ok"|"stopping", "platform": name}`.
- Signatures: `hmac_sha256_hex`, `hmac_sha256_base64`, `verify_hmac_sha256_hex(secret, body,
  offered, prefix=)`, `verify_hmac_sha256_base64`, `sha1_sorted_signature(*parts)`,
  `verify_sha1_sorted(offered, *parts)`, `hmac_sha1_url_signature(secret, url, params)`,
  `verify_url_signature(...)`, all comparing through `constant_time_equal`.
- `ReplayWindow(clock, window_s=300)`: `fresh(timestamp, unit=)`, `first_time(key)`,
  `accept(timestamp, key)`; `SeenIds(clock, ttl_s, max_entries)` for message-id dedupe.
- `failure_from_exception(exc)`, `failure_from_status(status, text, headers, now=, reading=)`,
  `retry_after_seconds(value, now)` → `SendResult` / seconds.
- Identity: `whatsapp_user_id`, `is_whatsapp_non_user`, `is_whatsapp_broadcast`,
  `strip_whatsapp_device`, `to_whatsapp_jid`, `WhatsAppIdentities` (`link`, `load_mapping_dir`,
  `aliases`, `canonical`, `same_user`, `allowlist_matches`); `bech32_decode`, `bech32_encode`,
  `npub_to_hex`, `hex_to_npub`, `nostr_pubkey`, `expand_nostr_allowlist`; `signal_number`,
  `signal_uuid`, `signal_sender` → `SignalSender(user_id, user_id_alt)`.

How a callback adapter uses the kit (the template is `tests/unit/gateway/adapters/http_sample.py`):
1. In `__init__`: `settings = ListenerSettings.from_extra(config.extra, port=<vendor default>,
   max_body_bytes=<vendor cap>, path=<default path>)` (a `ListenerError` there fails the factory,
   which the registry logs as "building the adapter failed"); `self.listener =
   CallbackListener(settings, name=self.platform, clock=self.clock, scrub=self.scrub,
   serve=serve)`; `self.listener.route(settings.path, self._callback, methods=("POST",))` (add a
   `GET` route for vendors with a verification handshake). Take `serve: bool = True` as a
   constructor keyword so the contract case can build it with `serve=False`.
2. `_open`: check the credentials over REST, then `await self.listener.start()` (raises
   `AdapterConnectError` itself: fatal for bad settings, retryable when the port is busy).
   `_close`: `await self.listener.stop()` first.
3. The handler: verify the vendor signature on `request.body` (or `request.signed_url(
   settings.public_url)` + `request.form_pairs()` for URL-signed vendors) with the `verify_*`
   helpers → `Reply.json({...}, 401)` on failure; `ReplayWindow.accept(timestamp, key)` /
   `SeenIds.first_time(message_id)` for replays and dedupe; parse (`request.json()` /
   `request.form()` raise `BadPayload` → 400); hand the event to
   `self.listener.background(self.emit(event))`; answer 200 at once.
4. Outbound: catch `httpx.HTTPError` → `failure_from_exception(exc)`; a status ≥ 400 →
   `failure_from_status(status, response.text, response.headers, now=self.clock.now(),
   reading=<the adapter's reading of a 404, if any>)`.
5. Identity: WhatsApp adapters keep one `WhatsAppIdentities` per adapter (`load_mapping_dir(<auth
   dir>)` at connect, `link(...)` when the bridge reports a pair) and emit `canonical(...)` as
   `user_id`; Buzz decodes its allowlist with `expand_nostr_allowlist`; Signal uses
   `signal_sender(sourceNumber or source, sourceUuid)` for `user_id` / `user_id_alt`.
6. Wire fake: subclass `HttpCallbackWire` (`tests/fakes/platform/http_wire.py`), add `@api(method,
   "/path/{segment}")` handlers that call the `FakeChatPlatform`, implement `render(item)` (vendor
   payload, signed), override `error_response` for the vendor's error shape; the contract case's
   `pump` is `await env.wire.pump(); await adapter.listener.idle()`; `build` calls
   `wire.attach(adapter.listener.app)` and gives the adapter `wire.client()`.

Files: `src/indus/gateway/adapters/_http.py`, `src/indus/gateway/adapters/_identity.py`,
`tests/fakes/platform/http_wire.py`, `tests/unit/gateway/adapters/test_http.py`,
`tests/unit/gateway/adapters/test_identity.py`, `tests/unit/gateway/adapters/http_sample.py`
(a sample callback adapter and its wire fake: the template later builders copy, run through the
whole shared contract), `tests/fixtures/gateway/nip19_vectors.json`. No shared file changes.

Edge cases / failure modes:
- non-loopback host without `webhook_public: true` → refused at start (fatal config, not
  retried); port outside 0–65535, a path not starting with `/`, or `/health` as a route → refused.
- port in use → `bind_listener` retries (P15), then a retryable connect error.
- `Content-Length` missing (chunked) → counted while reading, cut at the cap; non-numeric → 400.
- signature header missing → the check still returns False (never an exception); hex compared
  case-insensitively, base64 exactly; a non-ASCII offered value never raises.
- replay: a timestamp outside ±window → stale; a key seen inside the window → duplicate; a stale
  request never records its key (it cannot poison a later fresh one).
- a handler still running at stop gets the drain time, then is cancelled (its caller sees 503);
  background tasks are cancelled; the server stop is bounded by the budget and the socket is
  always closed.
- identity: empty, whitespace, full-width digits, path characters (`../`), unknown domains → not
  a user (None); a JID whose local part is not ASCII digits → None; `status@broadcast` is a
  broadcast; lid-mapping files with bad names, bad JSON, a BOM, or over 4 KiB are skipped.
- bech32: mixed case, a bad checksum, a character outside the charset, wrong hrp, wrong length,
  non-zero padding → None.

Our design differences / decisions:
- Our names (`CallbackListener`, `ListenerSettings`, `CallbackRequest`, `Reply`, `ReplayWindow`,
  `SeenIds`, `WhatsAppIdentities`, `SignalSender`).
- The kit is inside the adapters package (not core) and reuses the P15 serving helpers rather than
  a second server; no P15 file changes.
- Non-loopback binding needs the host **and** `webhook_public: true` (one typo cannot expose a
  callback port). See "Decisions for the lead".
- The canonical WhatsApp id prefers the phone-number member of an alias set (allowlists are
  written with phone numbers), then the shortest, then the smallest; the reference takes the
  shortest. Aliases live in an in-memory book per adapter that loads the bridge's mapping files on
  demand (the reference reads files on every check from a fixed global directory).
- The Nostr allowlist expansion keeps every original entry and adds the decoded hex (as the
  reference does), lower-casing hex entries.
- Outbound HTTP failures are classified by class and status, not by exception text, so an httpx
  connect timeout (whose text says "timed out") is retryable and a read timeout is never retried.
- `/health` is unauthenticated and says only `ok`/`stopping` and the platform name.
- No request-level auth in the kit: each adapter checks its vendor signature in its handler with
  the helpers (vendors differ in what they sign).

Tests we will write:
- settings: defaults, keys, public bind refusal, bad port/path.
- listener in-process: health, 404, 405, 503 before start and while stopping, 413 by declared
  length without reading, 413 mid-stream with the rest of the body unread, 400 bad length, 500 on
  a handler error, query/header/form/json access, signed URL.
- listener on a real loopback socket: serves, `/health`, stop within budget with a request still
  in flight, socket closed afterwards; second stop does nothing.
- signatures: known answers for each helper; the compare function is spied on (called for a bad
  signature); missing/garbage headers.
- replay window and seen ids on the fake clock (expiry, cap).
- outbound results: each httpx failure class and status → kind, `retryable`, `maybe_delivered`,
  `retry_after` (seconds and date forms).
- wire fake + sample adapter: the whole shared contract suite (every check, no real network);
  bad signature 401; stale timestamp refused; replayed callback dropped.
- identity: NIP-19 and BIP-173 vectors from the fixture; npub ↔ hex round trip; bad strings; JID
  with device suffix and its LID alias → one id; non-user suffixes; outbound JIDs; mapping files;
  Signal senders.

## Decisions for the lead
- Public bind needs `webhook_public: true` as well as a non-loopback `webhook_host`; drop the flag
  check in `ListenerSettings.check` to make the host alone enough.
- Listener keys live under `platforms.<name>` as free keys (`webhook_*`), read through
  `PlatformSettings.extra`: no schema rows added. If the lead wants typed rows, they are one
  `ListenerSection` reused by every callback platform.
- HTTP 502/504 from a vendor are read as "maybe delivered" (not retried), 503 as transient
  (retried), 500 as unknown; 401 is not "forbidden" (a wrong token must not mark the chat dead).
- Hard ceiling of 16 MiB on `webhook_max_body_bytes` whatever the config says.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] device suffix stripped before the domain; plus sign dropped (test:
  `test_identity.py::test_whatsapp_user_ids`)
- [x] group, broadcast and newsletter JIDs never users; status and newsletters never answered
  (test: `test_identity.py::test_non_user_and_broadcast_jids`,
  `::test_groups_and_broadcasts_never_join_an_alias_book`)
- [x] mapping files: forward and reverse names, BOM, safe names only, transitive links (test:
  `test_identity.py::test_mapping_files_from_the_bridge_auth_state`,
  `::test_links_are_transitive_and_the_phone_wins`)
- [x] outbound JID from a bare phone with separators, device suffix collapsed, other JIDs kept
  (test: `test_identity.py::test_device_suffix_and_outbound_jids`)
- [x] allowlist matching across phone/LID forms and `*` (test:
  `test_identity.py::test_allowlists_match_across_aliases`)
- [x] npub decode: hrp, bech32 (not bech32m) checksum, 32 bytes, padding (test:
  `test_identity.py::test_nip19_vectors_decode_and_encode`, `::test_npub_decoding_refuses_everything_else`)
- [x] npub allowlist entry authorises the hex pubkey; hex entries pass through (test:
  `test_identity.py::test_nostr_allowlists_authorise_the_hex_pubkey`)
- [x] Signal: number first, UUID as the alternate id, UUID alone when there is no number (test:
  `test_identity.py::test_signal_senders_keep_the_uuid_as_the_alternate_id`)
- [x] JID with a device suffix and its LID alias normalise to one id (Verify column; test:
  `test_identity.py::test_a_jid_with_a_device_suffix_and_its_lid_alias_are_one_person`)
- [x] oversized body refused without reading it fully (Verify column; test:
  `test_http.py::test_a_streamed_body_is_cut_at_the_cap_and_the_rest_is_never_read`,
  `::test_a_declared_oversized_body_is_refused_before_any_byte_is_read`)
- [x] bad signature 401 with the compare function spied (Verify column; test:
  `test_http.py::test_every_check_compares_in_constant_time`,
  `::test_callback_signature_replay_and_payload_checks`)
- [x] bech32 vectors from NIP-19 (Verify column; fixture `tests/fixtures/gateway/nip19_vectors.json`)
- [x] graceful stop inside the 5 s budget with a request in flight (test:
  `test_http.py::test_the_listener_serves_on_loopback_and_stops_within_the_budget`,
  `::test_stop_drains_then_cancels_a_stuck_handler_and_background_work`)
- [x] the wire fake base drives a whole contract run (test:
  `test_http.py::test_the_sample_callback_adapter_passes_the_contract`, `::..._uses_no_real_network`)
- Deliberate differences: the reference matches a raw allowlist entry first (so a group JID listed
  verbatim matches); our `allowlist_matches` is for users only and group allowlists compare chat
  ids exactly in the adapter. The reference lower-cases an npub before decoding (a mixed-case npub
  passes); we follow BIP-173 and refuse mixed case (all upper case is accepted). The reference
  takes the shortest alias as canonical; we prefer the phone number. Signal numbers and UUIDs are
  validated here (the reference passes them through), so a malformed number falls back to the
  UUID.
- Not taken: WhatsApp mention patterns, mention JIDs for outbound mentions, reply prefix and
  markdown conversion (adapter behaviour, P16-03/04); the reference's global session directory
  lookup (the adapter passes its own auth directory); Signal's legacy `source` field is the
  adapter's to pass as the number.

## Status

Done (2026-10-19). New files only, plus the R06.4 rows in `docs/internals/traps-ledger.md` (two
test references). No P15 core file, config schema, pyproject or lock change; no new dependency
(httpx, starlette and uvicorn are already direct dependencies).
