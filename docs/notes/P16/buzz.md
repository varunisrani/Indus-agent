# Task: Buzz adapter, Nostr community relay (P16-29) Phase: 16 Owner: wave-25 builder E10

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/buzz/adapter.py`
(whole file: settings, CLI plumbing, connect and seeding, sends and mention resolution, reactions,
WebSocket transport and NIP-42 auth, polling, cursors, DM discovery and classification, event
handling, attachments, thread anchoring), `plugins/platforms/buzz/nostr_auth.py` (skimmed: what it
signs, not how), `plugins/platforms/buzz/send_targets.py`, `plugins/platforms/buzz/plugin.yaml`,
`website/docs/user-guide/messaging/buzz.md`. Research 06 §3.3 (Buzz row), §3.5 (identity
normalisation). Kit: `docs/notes/P16/http-kit.md` (npub helpers).
Evidence: none.

Vendor API (interop facts): Nostr NIP-01 (event shape, id = SHA-256 of the compact JSON
`[0, pubkey, created_at, kind, tags, content]`, BIP-340 Schnorr signature over the id; client frames
`REQ`, `EVENT`, `CLOSE`, `AUTH`; relay frames `EVENT`, `EOSE`, `OK`, `CLOSED`, `NOTICE`, `AUTH`;
machine-readable prefixes `rate-limited:`, `blocked:`, `restricted:`, `invalid:`, `auth-required:`,
`duplicate:`, `error:`), NIP-10 (`e` tags with `root`/`reply` markers, `p` tags), NIP-19 (`npub`,
`nsec`), NIP-29 (kind 9 chat messages scoped by an `h` tag, group metadata kind 39000 `name`/`about`,
members kind 39002), NIP-42 (kind 22242 auth event with `relay` and `challenge` tags), NIP-94/92
(`imeta` tags: `url`, `m`, `x`, `size`), BIP-340 (the signature scheme and its test vectors). Buzz
specifics taken from the reference's behaviour: channels are UUIDs addressed by `h`; membership
events kind 44100 p-tag a new member; a DM is a conversation whose metadata name is `DM` with an
empty description.

Libraries: the relay WebSocket through httpx-ws 0.9.0 (MIT, already locked); BIP-340 signing and
verification through **coincurve 21.0.0** (MIT OR Apache-2.0, wraps libsecp256k1, MIT; uploaded
2025-03-08; wheels for CPython 3.13; no runtime dependencies), both in a new `buzz` extra and
imported lazily. No elliptic-curve maths in our code: `crypto.py` only serialises, hashes and calls
coincurve. The contract suite and most unit tests run without the extra through an injected
`NostrCrypto` test double (a keyed hash, test-only); the coincurve path has its own tests (BIP-340
vectors, a round trip) that skip when the extra is missing.

Capability flags: `max_len` 16,000 code points (the relay sets no documented cap; ours keeps an
event well under common relay limits), `splits_natively` true, `markup` markdown (Buzz renders
Markdown), `supports_edit` false, `supports_buttons` false, `reply_opens_thread` true,
`owns_access_policy` false, `auth_upstream` false, `dm_is_private` true, `push_capable` true.

What it does (own words): the bot is a Nostr identity (private key from the secret store, `nsec`
or hex) that is a member of a Buzz community relay. The adapter keeps one WebSocket to the relay,
answers a NIP-42 `AUTH` challenge whenever the relay sends one (also mid-session, then retries the
request the relay refused), and polls: every 4 s (configurable, never under 1 s) it asks each
watched channel for kind-9 events since its high-water mark, at most 50 per request. Every event
is checked before use: its id must be the hash of its contents and its signature must verify
against its pubkey; failures are dropped. Each channel remembers the last 500 event ids (dedupe)
and the authors of recent events (reply-to-us detection). Kinds other than 9 are ignored, as are
our own events. At connect each channel is seeded from its newest events (history is never
answered). Every 5th sweep discovers conversations: groups listing us as a member (kind 39002) and
membership events naming us (kind 44100); a new conversation whose metadata says `DM` is watched as
a DM from its start, a new named channel is watched (seeded) when no channel list is configured.
Channel messages carry `mentioned` when they p-tag us or name us (npub, hex or `@display name`); a
leading mention is stripped so commands work. A reply in a thread keeps the thread root as the
thread id; a top-level channel message offers its own id as the prospective thread, so the bot's
answer opens a thread under it (Buzz threads replies by default; `reply_in_thread: false` turns it
off). Sends publish signed kind-9 events with the `h` tag, NIP-10 tags and `p` tags for `@Name`
mentions resolved against the channel's member list (cached 60 s; profile names 5 min); the relay's
`OK` gives the result. Inbound files (`imeta`) are fetched lazily by the media intake for admitted
messages: HTTPS only, the relay's origin or a listed `attachment_hosts` origin, no redirects, the
declared size and SHA-256 must match.

Inputs → outputs:
- `BUZZ_RELAY_URL` (or `platforms.buzz.relay_url`), `BUZZ_PRIVATE_KEY` (secret store, then env).
  Keys: `channels` (list; empty = every channel we are a member of), `poll_interval` (4, floor 1),
  `reply_in_thread` (true), `attachment_hosts` ([]), `require_mention` (core gate, P15-11).
  Allowlist `INDUS_BUZZ_ALLOWED_USERS` / `allow_from` accept npub or hex.
- Relay events → `InboundEvent` (user id = lower-case hex pubkey, `user_id_alt` = its npub, chat id =
  channel UUID, chat type `group`/`dm`/`thread`).
- `send(channel, text)` → event ids.

Files: `src/indus/gateway/adapters/buzz/__init__.py`, `.../buzz/adapter.py`, `.../buzz/crypto.py`,
`tests/fakes/platform/buzz_wire.py`, `tests/unit/gateway/adapters/buzz/__init__.py`,
`tests/unit/gateway/adapters/buzz/test_adapter.py`, `tests/unit/gateway/adapters/buzz/test_crypto.py`,
`tests/fixtures/gateway/bip340_vectors.json`, `tests/contract/gateway/buzz_case.py`; shared:
contract `conftest.py` import line, `pyproject.toml` extra `buzz`, the `platforms.buzz` schema row +
its test line, THIRD_PARTY_NOTICES (coincurve row; httpx-ws/wsproto rows name `buzz`).

Edge cases / failure modes:
- key missing or not a valid `nsec`/64-hex in range → fatal config error (the key never appears in
  a log, error or repr); coincurve or httpx-ws missing → fatal `missing_dependency` with the hint.
- relay URL must be `https`/`wss` (or `http`/`ws` on loopback for local relays).
- an event whose id does not match its content, or whose signature fails, is dropped and logged once
  per event id at debug.
- `AUTH` challenge answered; an `OK false` to it → `report_fatal("buzz_auth")` (the identity is not
  a member) and the platform is parked.
- `CLOSED` with `restricted:`/`auth-required:` after auth → the channel is dropped from the watch
  list (no reconnect loop).
- publish answers: `rate-limited:` → rate limited (wait parsed when the text names one), `blocked:`
  / `restricted:` → forbidden, `invalid:` → bad format, no `OK` within 15 s → maybe delivered (never
  retried), socket write failure → transient.
- a target that is a pubkey/npub is sent to the DM conversation with exactly that member and us,
  found through the member lists; none → "chat not found".
- an `imeta` with http, credentials in the URL, an unlisted origin, a missing or wrong size/SHA-256
  → refused by the fetcher; more than 4 attachments → the rest dropped.

Our design differences / decisions:
- No `buzz` CLI: we speak Nostr to the relay ourselves (WebSocket polling and signed publishing),
  so no binary, no subprocess, no key in a child environment.
- Signature verification on every inbound event (the reference trusts the CLI's output).
- Allowlists: npub entries authorise the hex pubkey through `resolved_allowlist_ids` (env list) and
  through `user_id_alt` = npub (config lists, pairing records).
- No persisted cursors: a restart seeds from the newest events, so messages sent while the gateway
  was down are not answered (the reference persists cursors). Follow-up.
- No edits, deletes, reactions or outbound files (the relay's edit and upload APIs are Buzz-specific
  and undocumented here); no forum kinds 45001/45003; no NIP-OA auth tag; no reaction-only users;
  media served by the relay behind auth is not fetched (public URLs only).

Tests we will write:
- contract suite over the fake relay (fake crypto).
- npub allowlist entry authorises the matching hex pubkey through the real authorizer.
- non-chat kinds ignored; own events ignored; bad id / bad signature dropped; dedupe and seen cap
  500; seeding never dispatches history; mention detection and stripping; reply-to-us; threads.
- poll interval floor 1 s, 50 per request, DM discovery every 5 sweeps, member cache 60 s.
- NIP-42 auth answered; auth refused → fatal; restricted channel dropped.
- publish classification; key never logged; `imeta` fetch checks.
- coincurve path: BIP-340 vectors verify, a signed event round-trips (skipped without the extra).

## Decisions for the lead
- coincurve 21.0.0 in a `buzz` extra (licence MIT OR Apache-2.0, both allowed; libsecp256k1 MIT)
  for BIP-340; no alternative in the lock (`cryptography` has no BIP-340).
- Registry row: name `buzz`, label `Buzz`, probe `coincurve` + `httpx_ws` (extra `buzz`),
  `required_env` `BUZZ_RELAY_URL` + `BUZZ_PRIVATE_KEY`, max_len 16,000, install hint "pip install
  'indus-agent[buzz]'"; `BUILTIN_CREDENTIALS["buzz"] = ("BUZZ_PRIVATE_KEY",)` (the token lock keys
  on the identity, so two gateways on one key conflict).
- Cap 16,000 and DM detection through kind 39000 metadata (name `DM`, empty `about`) are our
  choices; check them against Buzz's documentation when a live relay is available.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] seeding at connect; polls since the high-water mark (inclusive, deduped by id); 50 per poll
  (tests: `test_adapter.py::test_history_is_seeded_not_answered_and_events_are_deduped`,
  `::test_polls_ask_for_50_chat_events_since_the_mark_at_the_configured_pace`)
- [x] seen set 500 per channel; authors kept for reply-to-us (test: `::test_threads_and_replies_follow_nip10`)
- [x] DM discovery every 5 sweeps; DM = metadata named `DM` with no description; watch-all adoption
  of new channels (test: `::test_dm_discovery_runs_every_fifth_sweep_and_dms_start_from_their_beginning`)
- [x] mentions by npub, hex or `@name`, a leading one stripped, p-tag addressing (test:
  `::test_mentions_are_detected_and_a_leading_one_is_stripped`)
- [x] thread roots (NIP-10 markers, deprecated positional form) (test: `::test_threads_and_replies_follow_nip10`)
- [x] NIP-42 auth event: kind 22242, `relay` + `challenge` tags (tests:
  `::test_the_auth_challenge_is_answered_with_a_signed_event`, `::test_a_refused_identity_is_fatal`)
- [x] `CLOSED restricted` channels dropped (test: `::test_a_restricted_channel_is_dropped_from_the_watch_list`)
- [x] outbound `@Name` p-tags from the member list (60 s) and names (5 min); ambiguous names tag
  nobody; longest names first (test: `::test_mentions_out_use_the_member_list_cached_for_60_seconds`)
- [x] `imeta`: https, trusted origin, no credentials or fragment, size and SHA-256, ≤4, 20 MiB in
  all (test: `::test_imeta_files_are_fetched_with_their_checks`)
- [x] gap found: a socket the relay stops answering would leave the poller timing out forever (the
  reference has a 300 s read-idle bound on its WebSocket) — a poll without an answer now drops the
  socket and the runner reopens it (test: `::test_a_relay_that_stops_answering_is_reconnected`)
- [x] Verify column: contract suite; npub allowlist entry authorises the hex pubkey
  (`::test_an_npub_allowlist_entry_authorises_the_matching_hex_pubkey`); non-chat kinds ignored
  (`::test_non_chat_kinds_and_our_own_events_are_ignored`); signatures verified
  (`::test_signatures_are_verified_on_inbound_events`, `test_crypto.py::test_bip340_vectors_verify_as_the_spec_says`)
- Deliberate differences: no buzz CLI (our own Nostr client), signature checks on every inbound
  event, polling over one authenticated WebSocket (the reference prefers a push subscription and
  falls back to CLI polls), npub allowlists through `resolved_allowlist_ids` and `user_id_alt`.
- Not taken: persisted cursors (messages sent while the gateway is down are not answered —
  follow-up), edits/deletes/reactions (the 👀 "seen" reaction too), outbound files, forum kinds
  45001/45003, NIP-OA auth tags, reaction-only users, the credentials-file fallback, DM latching
  from p-tagged messages (classification is by metadata only), the setup wizard (P16-31).

## Status

Done (2026-10-20). Files: the adapter package (`adapter.py`, `crypto.py`), the fake relay (with the
test-only `FakeNostrCrypto`), unit tests, contract case, `tests/fixtures/gateway/bip340_vectors.json`
(the BIP-340 CSV rows with 32-byte messages, converted to JSON). Shared: contract `conftest.py`
import line, `pyproject.toml` extra `buzz`, THIRD_PARTY_NOTICES (coincurve row; httpx-ws / wsproto
rows name `buzz`), `platforms.buzz` schema row and its test line, ACC-K12-10. `uv.lock` not
regenerated (lead): the `buzz` extra adds coincurve 21.0.0. Not done here (P15 core, for the lead):
the registry row and `BUILTIN_CREDENTIALS` entry above. The production opener and coincurve signer
were smoke-tested once by hand against a local WebSocket relay stand-in (scratch script).
