# Task: WhatsApp bridge adapter (P16-03) Phase: 16 Owner: wave-27 builder E13

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/whatsapp/adapter.py`
(init, pidfile helpers, connect / adopt / foreign-session handling, disconnect, send, typing,
poll loop, event building), `plugins/platforms/whatsapp/bridge_ownership.py` (whole file),
`plugins/platforms/whatsapp/gateway_env.py`, `plugins/platforms/whatsapp/plugin.yaml`,
`gateway/platforms/whatsapp_common.py` (allowlist selection, mention gating and
`_should_process_message`, lines 60–268).
Evidence: none.

Vendor API: our own bridge (P16-02, `apps/whatsapp-bridge`), HTTP on loopback; routes and event
shape in `whatsapp-bridge-sidecar.md`. No Python library: `httpx` (base dependency); no extra.

Capability flags: `max_len` 4,096 code points (`splits_natively`), `markup` markdown with the
WhatsApp pass of `whatsapp_cloud.markup.to_whatsapp` per piece, `supports_edit` false (streaming
falls back to segments), `supports_delete` false, `supports_buttons` false (approvals are the text
card), `owns_access_policy` true, `auth_upstream` false, `dm_is_private` true, `push_capable` true,
`slot_spacing` 0.3 s (the reference paused 300 ms between pieces), `inline_wait_cap` 60 s (base).

Files: `src/indus/gateway/adapters/whatsapp/__init__.py` (`create_adapter`,
`default_bridge_script`), `adapter.py`, `sidecar.py`; `tests/fakes/platform/whatsapp_wire.py`
(fake bridge, `FakeLauncher`, `FakeSpawner`); `tests/contract/gateway/whatsapp_case.py`;
`tests/unit/gateway/adapters/whatsapp/test_adapter.py`, `test_sidecar.py`, `test_real_bridge.py`
(the real Node bridge with its fake provider, skipped without Node). Shared: one import line in
`tests/contract/gateway/conftest.py`; the `platforms.whatsapp` row in
`foundation/config/schema.py` and its pin in `tests/unit/foundation/config/test_schema.py`; trap rows
R02.22 / R06.22 and ACC-K9-1.

What it does (own words): the adapter owns the bridge process. On connect it adopts its own bridge
when one is still running (pidfile with pid, kernel start time, port and profile; the token file next
to it; `/health` answers 200 with our profile), else starts a fresh one with `node <bridge>` and an
environment built by `safety.child_env(variant="minimal")` plus the `INDUS_WA_*` values, waits for
`/health` to say `open`, and polls `/events` (long-poll, cursor). A bridge that dies is restarted
after 2 s, doubling to 60 s (reset after 60 s up). A port held by anything it cannot prove is its own
(no pidfile of ours, or `/health` refuses our token or names another profile) is a fatal
`whatsapp_bridge_port_taken`; it is never killed. An unpaired bridge (`pairing`) is a fatal
`whatsapp_not_paired` (pairing runs from `indus gateway setup whatsapp`, P16-31); a logged-out
session reports `whatsapp_logged_out`. `indus send` (send-only) uses the running bridge of this
profile and never starts one (a temporary one would swallow inbound messages); without one the
connect fails with `whatsapp_bridge_not_running`.

Ports: `platforms.whatsapp.bridge_port`, else 3000 for the default profile, else the profile's
recorded port (`<state>/bridge_port`), else the first port in 3001–3999 that no sibling profile has
recorded and that binds on 127.0.0.1; the choice is recorded.

State folder: `<home>/platforms/whatsapp/<profile id>/` (0700): `auth/` (the bridge's auth state),
`bridge.pid` (JSON), `bridge.token` (0600), `bridge_port`, `bridge.log` (0600). `platforms/` is not
copied by profile clones (R04.26).

Inbound (one bridge event → at most one `InboundEvent`):
- sender id = `WhatsAppIdentities.canonical` (phone digits when known, LID digits otherwise), alt
  id = the first other alias (sorted); LID↔phone links from the event's `senderAlt` and the bridge's
  `lid-mapping-*.json` files in the auth folder (reloaded at most once a minute when an unknown LID
  appears); chat id = the JID (device suffix stripped), `group` for `@g.us`;
- own policy, before emit: DM `dm_policy` (`pairing` default, `open`, `allowlist`, `disabled`) and
  group `group_policy` (`open` default, `allowlist`, `disabled`); `disabled` drops; group `allowlist`
  passes a chat whose JID is listed in `group_allow_from` or a sender matching it by any alias; the
  core asks `access_policy` / `allows_dm` (DM `allowlist` decided here with `allow_from` by alias);
- mention gating in groups (`require_mention`, default on): a group message passes only when it
  starts with `/`, mentions us, quotes one of our messages, matches `mention_patterns`, or the chat
  is in `free_response_chats`; our `@<digits>` mention is removed from the text;
- media: one `Attachment` per item, ref `wa-media:<ref>` fetched through the registered fetcher
  (`GET /media`), kinds voice (push-to-talk) / audio / photo / video / document / sticker; text
  documents ≤100 KiB are inlined by the intake (P15-23 `INLINE_TEXT_MAX_BYTES`), voice notes go to
  the voice intake;
- edits marked `edited`; the owner's self-chat messages marked `note_to_self`; replies carry
  `reply_to_id`, `reply_to_text`, `reply_to_author_id`, `reply_to_is_own`.

Outbound: `/send` per piece (`reply_to` on the first), `/send-media` with the file path, `/typing`
composing/paused, `/read` after an admitted message when `send_read_receipts` is on, `react()`.
Failures: connect errors retryable, read timeouts and 504 maybe-delivered (never resent), 429 with
the bridge's `Retry-After`, 404 `chat_not_found` chat-level, `message_not_found` sub-chat, 403
forbidden, 400 bad format, 413 too long, 503 transient.

Logs: phone numbers masked (`mask_numbers` in `scrub`), the token scrubbed, message text never
logged.

Edge cases / failure modes:
- node missing / bridge script missing → fatal non-retryable (`whatsapp_node_missing`,
  `whatsapp_bridge_missing`);
- pidfile of ours whose process is gone or recycled (start time differs) → removed, port probed;
- our own bridge alive but unhealthy (no 200 in the wait) → terminated (it is ours by fingerprint)
  and started again;
- bridge crash while running → restart with back-off; the cursor resets when `epoch` changes;
- two profiles on one port → the second gets `whatsapp_bridge_port_taken`, the first is untouched.

Our design differences / decisions:
- the bridge is spawned with `child_env(variant="minimal")` (R02.22) and our `INDUS_WA_*` names;
  secrets never in argv;
- pidfile is JSON with the profile id and port, plus the token file (the reference had no token);
- `require_mention` defaults to true in groups (the reference default is off): a bot added to a
  group answers only when addressed;
- media fetched lazily (Signal's pattern) instead of the bridge writing into the cache;
- no reply prefix, no owner-forwarding mode, no polls / locations / text-batch debounce.

Tests we will write:
- contract suite over `tests/fakes/platform/whatsapp_wire.py` (`whatsapp_case.py`);
- a second profile cannot adopt the first profile's bridge (port taken, 401 / foreign profile);
- crash restarts with back-off 2, 4, 8 … 60 s on the fake clock;
- group message without a mention ignored; with mention / slash / quote passes; policies;
- LID/phone aliases give one user id; allowlist by alias;
- send-only uses a running bridge and never spawns, fails without one;
- spawn env built by `child_env` minimal (no provider keys), token not in argv, files 0600;
- numbers masked in logs; fetcher; read receipts; port allocation.

## Decisions for the lead
- Registry row (P15 core, not edited here; proposed): `PlatformEntry("whatsapp", "WhatsApp",
  factory=lazy("indus.gateway.adapters.whatsapp:create_adapter"), check_fn=modules_installed("httpx"),
  required_env=(), allowlist_env INDUS_WHATSAPP_ALLOWED_USERS …, max_len=4096, dm_is_private=True,
  hint "You are talking through WhatsApp. Markdown is converted to WhatsApp formatting; avoid
  tables.", install_hint "install Node.js 24 and the WhatsApp Web provider, then run `indus gateway
  setup whatsapp`")`; `BUILTIN_NAMES` gains `whatsapp` before `whatsapp_cloud`. No
  `BUILTIN_CREDENTIALS` / `BUILTIN_PUBLIC_ENV` row: there is no credential, the platform turns on with
  `platforms.whatsapp.enabled: true` (the "enabled in config" path). No lock key (`lock_env` empty
  and no credential → no token lock); the per-profile auth folder and the pidfile ownership check
  stand in for it.
- Config: `platforms.whatsapp` row added to `PlatformsSection` (shared, one line + test pin).
- The bridge script path: `platforms.whatsapp.bridge_script`, else `apps/whatsapp-bridge/src/main.ts`
  found from the package location in a source checkout. Packaging the bridge for wheel installs is a
  follow-up (P23/P25).

Compare checklist (filled after the rewrite; reopened `adapter.py` connect / disconnect / poll /
event building, `bridge_ownership.py`, `whatsapp_common.py` gating):
- [x] pidfile identity is pid + kernel start time; without a start time nothing is ours; a stale or
  recycled pidfile is removed and its pid never signalled (test: `test_sidecar.py::
  test_a_recycled_pid_is_not_ours_and_an_unhealthy_bridge_of_ours_is_replaced`)
- [x] a foreign bridge on the port is never adopted and never killed (tests: `test_sidecar.py::
  test_a_second_profile_cannot_adopt_or_stop_the_first_profiles_bridge`,
  `::test_a_port_answered_by_someone_elses_bridge_is_left_alone`)
- [x] secondary-profile ports 3001–3999 skip sibling records and bound ports; recorded ports are
  reused; the default profile keeps 3000 (test: `test_sidecar.py::
  test_ports_default_secondary_recorded_and_sibling_claims`)
- [x] adopt only on the recorded port with a healthy answer for our profile (test: `test_sidecar.py::
  test_a_restarted_adapter_adopts_its_own_running_bridge`)
- [x] stop: TERM, then KILL after a grace period; pidfile removed (test: `test_sidecar.py::
  test_stop_ends_our_bridge_and_forgets_its_files`)
- [x] mention gating: slash commands, mentions, replies to us, patterns, free-response chats,
  `require_mention`; our mention removed from the text (tests: `test_adapter.py::
  test_a_group_message_without_a_mention_is_ignored`, `::test_free_response_chats_and_…`)
- [x] broadcasts never answered (test: `test_adapter.py::test_broadcasts_are_dropped_and_…`)
- [x] DM / group policies and allowlists by alias (tests: `test_adapter.py::
  test_disabled_and_allowlist_policies`, `::test_the_core_reads_the_own_policy_and_…`,
  `::test_lid_links_come_from_the_bridges_mapping_files`)
- [x] media paths: the adapter never takes a path from the bridge (refs fetched through the
  intake's cache); text documents inlined there (test: `test_adapter.py::
  test_media_refs_are_fetched_lazily_and_documents_reach_the_intake`)
- [x] gap found: the reference paused 300 ms between pieces — `slot_spacing` 0.3 s
- [x] gap found: our own inbound helper shadowed the base `_media` hook, so every media send
  failed — renamed (`_attachments`), pinned by `test_adapter.py::test_media_goes_by_resolved_path`
- Deliberate differences: the adapter restarts a dead bridge itself with back-off 2→60 s (the
  reference reported a retryable fatal for the daemon's watcher); an unpaired profile is detected
  from the bridge's `pairing` status, not by looking for the library's `creds.json`; the port holder
  is never killed (the reference's default profile terminated any `node` listener on its port after a failed
  probe);
  `require_mention` defaults to on; no reply prefix; owner messages only in self-chat; the poll
  is a cursor long-poll (25 s) instead of a 1 s drain loop.
- Not taken: native polls for clarify prompts, locations, edits, chat info lookups (`/chat/:id`),
  quoted-media folding, text-burst debounce, the `WHATSAPP_ENABLED` env flag (config enables it),
  automatic `npm install` / Node install from the adapter, the script-hash staleness check.

## Decisions for the lead (additional)
- `user_id_alt` is the other alias, so `session_keys.canonical_participant` (which prefers it)
  keys a group participant by that alias; a link learned later (LID ↔ phone) can move a group
  participant's per-user session once. A WhatsApp fold in `canonical_participant` (the seam it names)
  would make it stable — core follow-up.
- `indus send` through the real `send_command.build_adapter` needs the registry row; once the lead
  adds it, a `delivery/test_send_p16_platforms.py` case like Signal's can wrap `create_adapter` to
  pass `FakeLauncher` + the fake bridge transport.
- Provider: `platforms.whatsapp.provider` or `INDUS_WHATSAPP_PROVIDER` names the operator-supplied
  provider module (exports `createWaSocket`; P16-02 note); without one the bridge exits 2 and the
  adapter raises the non-retryable `whatsapp_provider_missing` (added at integration); P16-31's setup step should run the bridge in pair mode
  (`INDUS_WA_MODE=pair`, optional `INDUS_WA_PAIR_PHONE`) and print the `qr` / `pairing_code` events.

## Status

Done (2026-10-20). 34 unit tests (`tests/unit/gateway/adapters/whatsapp`, one of them drives the
real Node bridge) and the shared contract suite over the fake bridge pass. Registry row and
`indus send` wiring are for the lead (above).

Landed at integration (wave 27): registry row, target rule, setup step (points elsewhere) and an `indus send` case (D-16.531 … D-16.533, D-16.537, D-16.538); without a provider the bridge exits 2 and the adapter raises the non-retryable `whatsapp_provider_missing` (`test_sidecar.py`, `test_real_bridge.py`).
