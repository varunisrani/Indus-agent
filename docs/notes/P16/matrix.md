# Task: Matrix adapter, plain (P16-06) Phase: 16 Owner: wave-23 builder E2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/matrix/adapter.py`
(module notes, size budget and length unit, connect / whoami, initial sync and sync loop, the message
handler and its gating, reply fallback, DM detection, mentions, relation metadata, edit, typing,
reactions, media up/download, HTML conversion and sanitiser, standalone send), `matrix/voice_mention.py`
(skim: parked voice notes), `matrix/plugin.yaml`, `matrix/gateway_env.py`, `matrix/send_targets.py`,
`matrix/adapter_hint.py`; test names only in `tests/gateway/test_matrix*.py`. Research 06 §3.3 (row
Matrix), §3.5 (mention gating). Evidence: none.

What it does (own words): connects Indus Agent to any Matrix homeserver with a bot account's access
token. Inbound, a long-poll `/sync` loop reads room timelines; each human message becomes one
`InboundEvent`: rooms are groups, rooms listed in the `m.direct` account data (or with two joined
members) are DMs, `m.thread` relations are threads, `m.in_reply_to` gives the reply context (the
quoted fallback text is cut off the body), mentions of the bot (intentional `m.mentions`, the full
user id, `@localpart`, a matrix.to pill, or a reply to the bot) mark the event `mentioned` for the
core's mention gating. Events sent more than 5 s before the listener started (the backlog an
initial sync replays) are dropped, as are our own events, edits, bridge ghosts (`@_…`) and users
matching `ignore_user_patterns`. Outbound, text goes as `m.room.message` with a Markdown `body` and an
`org.matrix.custom.html` `formatted_body`, cut by UTF-8 bytes so the event stays under the server's
65,536-byte limit; edits use `m.replace` (streaming works), deletes are redactions, typing uses the
room typing endpoint, media are uploaded to the content repository and sent as `m.image` / `m.file`
/ `m.audio` (voice flag) / `m.video`. Room aliases (`#ops:example.org`) and user ids (`@ana:…`, sent
to the DM room `m.direct` names) resolve for cron and `indus send` targets.

Inputs → outputs: `AdapterConfig` (`MATRIX_ACCESS_TOKEN` secret, `MATRIX_HOMESERVER` secret/env or
`platforms.matrix.homeserver`, optional `MATRIX_USER_ID` / `MATRIX_DEVICE_ID`, `platforms.matrix.*`
options) → a connected adapter; `/sync` JSON → `InboundEvent`s through `emit`; `send` / `edit` /
`delete` / media / typing calls → Client-Server API requests → `SendResult`s.

Files: `src/indus/gateway/adapters/matrix/__init__.py` (factory, target splitter),
`src/indus/gateway/adapters/matrix/adapter.py` (the adapter), new `client.py` (our Client-Server API
client over httpx, its error readings and route table), new `html.py` (Markdown → Matrix HTML),
`tests/fakes/platform/matrix_wire.py` (fake homeserver behind `httpx.MockTransport`),
`tests/contract/gateway/matrix_case.py` (contract case, moved there at integration), `tests/unit/gateway/adapters/matrix/test_adapter.py`; shared:
`tests/contract/gateway/conftest.py` (case registration), traps ledger R06.11 row.

Vendor API: Matrix Client-Server API **v1.11** (`/_matrix/client/v3/...`; authenticated media
download `/_matrix/client/v1/media/download/...` with the legacy `/_matrix/media/v3/download/...`
fallback; upload `/_matrix/media/v3/upload`). No library: plain HTTP over httpx (already a core
dependency). Routes used (`client.ROUTES`): `GET account/whoami`, `GET sync`,
`PUT rooms/{room}/send/{type}/{txn}`, `PUT rooms/{room}/redact/{event}/{txn}`,
`PUT rooms/{room}/typing/{user}`, `GET|PUT user/{user}/account_data/{type}`,
`GET directory/room/{alias}`, `POST join/{room}`, `GET rooms/{room}/state/{type}/{key}`,
`POST media upload`, `GET media download`.

Capability flags (research 06 Q4): `dm_is_private` true (a Matrix DM is a two-person room),
`owns_access_policy` false (the core chain decides; the adapter only filters `allowed_rooms`),
`auth_upstream` false, `supports_edit` true, `supports_delete` true, `supports_buttons` false
(approvals as text with `/approve`), `push_capable` true, `splits_natively` true, cap 15,000 UTF-8
bytes per piece (`len_fn` = UTF-8 bytes), markup `markdown`.

Edge cases / failure modes:
- 429 `M_LIMIT_EXCEEDED` carries `retry_after_ms` → rate limited with the server's wait.
- `M_UNKNOWN_TOKEN` / 401 at connect → fatal (`retryable=False`); during sync → the loop stops and the
  lost handler is told `fatal`; on a send → a final failure (never a dead chat).
- `M_FORBIDDEN` → forbidden (dead chat); `M_NOT_FOUND` on a room → chat not found, on an event →
  message not found (the chat stays alive); `M_TOO_LARGE` → too long; `M_BAD_JSON` → bad format;
  502/503/504 → connection-level (retried); a read timeout is never retried (R06.4).
- A text over the cap is split by bytes with the core chunker; a partly delivered split reports
  the undelivered remainder, never the whole text (R06.4). A chunk whose HTML pushes the event
  content over 45,000 bytes goes out without `formatted_body`.
- Encrypted rooms without the E2EE extra: `m.room.encrypted` events are dropped with one warning per
  room (never passed on as empty text).
- Inbound media with a non-`mxc://` URL or a declared size above the cap are dropped; the download
  streams with the byte cap enforced.
- Send-only open (`indus send`): connect is `whoami` only; the sync loop starts only when an inbound
  sink is installed, so a one-shot send never reads (or advances) the timeline.
- Invites: joined only from users the configured allowlists name (or paired users), or from anyone
  with `invites: all`; `invites: off` never joins.

Our design differences / decisions:
- Own small Client-Server API client over httpx instead of mautrix for the plain adapter: the shipped
  code path is the tested one (the wire fake swaps only the transport), no SDK import, nothing to
  pin. mautrix (MPL-2.0) stays the planned E2EE library of P16-07 behind the `EndToEnd` seam below.
- E2EE seam (P16-07): `adapter.EndToEnd` protocol — `absorb_sync(sync)` (to-device events, device
  lists, one-time key counts), `decrypt(room_id, event) -> event | None`,
  `encrypt(room_id, event_type, content) -> (event_type, content)`, `is_encrypted(room_id)`,
  `aclose()`. `create_adapter` asks `matrix.crypto.create_end_to_end(config, adapter)` when
  `platforms.matrix.e2ee` is `optional` / `required`; without that module `required` fails the
  connect as fatal with an install hint and `optional` runs plain with one warning.
- No password login (the phase asks for token login); the reference's auto-threading of every room
  message is opt-in (`auto_thread: true`), default off: replies go to the room (with `m.in_reply_to`
  per `reply_to_mode`), threads only where the user started one.
- `m.notice` events are passed on marked `is_bot` (the core's bot rules decide) instead of being
  dropped.
- Approvals by reaction, voice-note parking, LaTeX `data-mx-maths`, read receipts, presence, room
  creation and the clock-skew warning are not built (see Status).

Tests we will write:
- contract suite over the wire fake (all rules, no real network);
- backlog event older than 5 s before start ignored, a fresh one delivered;
- thread reply keyed by the thread root; reply fallback stripped and quoted text kept;
- 46,000-byte reply split by bytes as well as characters, every event under 45,000 content bytes;
- edits use `m.replace`; redaction; typing on/off; alias and user-id target resolution;
- error readings (rate limit wait, unknown token final, not-found readings);
- send-only open does no `/sync`; listener starts on `set_sink`;
- DM detection by `m.direct`; mention detection forms; bang commands; bridge ghosts dropped;
- media in/out (upload + event, fetcher streams with a cap, non-mxc URL refused);
- token never in logs, errors or repr; client route table served by the wire fake.

## Decisions for the lead
- Registry row and credential table are P15 core files (not edited): proposed
  `_chat_entry("matrix", "Matrix", sdk=("httpx",), extra_name="matrix",
  required_env=("MATRIX_ACCESS_TOKEN", "MATRIX_HOMESERVER"), max_len=15000, hint=...)` in
  `registry.builtin_entries` and `"matrix": ("MATRIX_ACCESS_TOKEN",)` in `config.BUILTIN_CREDENTIALS`
  (the homeserver is read separately so it is not masked in every log line). Until then the
  platform runs with `platforms.matrix.enabled: true` and is built by `create_adapter`.
- Own HTTP client instead of mautrix for plain Matrix (above); no `matrix` extra is needed until
  P16-07.
- `auto_thread` default false (reference: true).
- Inbound media refs are the `mxc://` URIs; the intake gets the bytes from `fetch_media`.
- Config rows: `platforms.matrix` and `platforms.mattermost` added to `PlatformsSection` (schema
  test updated) so the loader stops logging "unknown setting" for them.
- Proposed P15 seams (not built; adapters carry the pieces): an `Adapter` lost-connection hook the
  daemon wires to `disconnect_platform` + the reconnect watcher (today `adapter.lost_handler`); a
  processing-state hook (today `processing_started` / `processing_finished`); fetcher registration
  when a platform connects (today `adapter.register_fetchers(intake)`); an `html` markup in
  `outbound.formatting` so Matrix could share the core renderer (today `matrix/html.py`).
- `m.notice` events reach the core marked `is_bot` (reference: dropped unless `process_notices`).
- Target addresses: `split_target("!room:server:$event")` → room + thread (the room id keeps its
  colons) for P16-32 to register with `cron.targets.register_platform("matrix", splitter=...)`.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/matrix/adapter.py`,
`voice_mention.py`, `send_targets.py`):
- [x] Token login by `whoami`; a refused token is fatal, other failures retryable
  (`test_adapter.py::test_connect_failures_are_classified`).
- [x] Backlog older than 5 s before the listener started dropped
  (`::test_backlog_event_at_startup_is_ignored`, `::test_an_event_inside_the_grace_window_is_kept`).
- [x] Own events (any case, and all events while our id is unknown), bridge ghosts, ignore patterns,
  repeats (last 1,000), edits and unknown msgtypes dropped (`::test_dropped_senders_and_events`).
- [x] DMs by `m.direct` or two joined members (`::test_dm_detection_by_m_direct_and_member_count`);
  `allowed_rooms` with DMs exempt (`::test_allowed_rooms_drop_other_rooms_but_not_dms`).
- [x] `m.thread` threads keyed by the root, answers go back with an `m.thread` relation
  (`::test_thread_reply_keys_match_the_thread`); a chosen reply inside a thread is not a fallback
  (`::test_a_reply_inside_a_thread_is_not_a_fallback`).
- [x] Reply fallback cut, quoted text and author (and the author's name) kept, a reply to the bot
  counts as a mention (`::test_reply_fallback_is_cut_and_a_reply_to_us_counts_as_a_mention`).
- [x] Mentions: `m.mentions`, full id, `@localpart`, matrix.to pill; a bare localpart word is not a
  mention (`::test_mentions_are_detected_and_removed`).
- [x] Gap found: the reference lets a thread the bot takes part in through without a new mention
  (`thread_require_mention` off by default) — added (`::test_a_thread_the_bot_takes_part_in_needs_no_new_mention`).
- [x] Gap found: stripping `@bot:server:` left a stray colon — fixed in `_strip_mention`.
- [x] Gap found: on a device-id clash the token's device must win (keys belong to it) — fixed,
  with a warning.
- [x] `!command` → `/command` only for known commands
  (`::test_notices_are_bots_and_bang_commands_become_slash_commands`).
- [x] Media: non-mxc URL refused, declared size over the cap refused, bare filename body is no
  caption, voice flag (`::test_media_events_and_the_fetcher`); download streams with a cap and the
  legacy route as a fallback (`::test_fetcher_caps_the_size_and_falls_back_to_the_legacy_route`).
- [x] Outbound: UTF-8 byte unit, 15,000 per piece, content ≤ 45,000 bytes, HTML dropped when it
  would burst the event (`::test_a_46000_byte_reply_is_split_by_bytes_as_well_as_chars`,
  `::test_html_that_would_burst_the_event_is_left_out`); intentional mentions, `@room` only when
  allowed (`::test_text_goes_with_html_and_intentional_mentions`, `::test_room_mentions_only_when_allowed`).
- [x] Edit as `m.replace` with `m.new_content` and a `* ` fallback; redaction; typing 30 s / off
  (`::test_edits_use_m_replace_and_deletes_redact`, `::test_typing_turns_on_and_off`).
- [x] Media out: upload, `m.file`/`m.image`/`m.audio`/`m.video`, caption with `filename`, voice flags
  (`::test_media_upload_then_event`), cap before upload (`::test_media_over_the_cap_is_refused_before_upload`).
- [x] Invites only from allowed users, a direct invite recorded in `m.direct`
  (`::test_invites_join_only_from_allowed_users`).
- [x] Encrypted rooms without E2EE: one warning per room, nothing passed on
  (`::test_encrypted_rooms_are_skipped_with_one_warning`); required E2EE without the module is
  fatal with a hint (`::test_required_e2ee_without_the_module_is_fatal_with_a_hint`).
- [x] Sync errors back off; a refused token stops inbound and reports the platform lost
  (`::test_the_sync_loop_backs_off_and_stops_on_a_refused_token`).
- [x] Standalone send (the reference's out-of-process sender) = our send-only open
  (`::test_send_only_open_never_syncs_and_the_sink_starts_the_loop`,
  `::test_indus_send_over_the_wire_does_no_inbound_work`).
- [x] Contract suite (`tests/contract/gateway`, case `matrix`): all rules, no real network.
- Deliberate differences: our own HTTP client, not mautrix (above); sync errors back off 2 s → 60 s
  instead of a flat 5 s; `m.notice` passed on as a bot message (the core's allow-bots rule decides)
  instead of dropped; `auto_thread` off by default; a reply relation only on the first piece of a
  split reply; `is_falling_back` false when the answer replies to a specific event in a thread;
  HTML is generated escaped from the Markdown (no raw HTML pass-through, so no sanitiser needed);
  any room in `m.direct` counts as a DM (the reference also asked for an unnamed room); the working
  reaction is removed at once (the reference waits 5 s).
- Not taken (recorded): password login; approvals and pickers by reaction (approvals go as text
  with `/approve`); voice-note parking (Element X mention after a voice note); LaTeX
  `data-mx-maths`; read receipts; presence; room creation and invites by the bot; the clock-skew
  warning; DM auto-threads and `session_scope`; a configurable chunk size (fixed 15,000 bytes);
  leaving a room the server says is gone; skill commands for `!command` (only registry commands).

## Status

Done (2026-10-19). Shared files touched: `tests/contract/gateway/conftest.py` (P16 case
registration), `src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py`
(`platforms.matrix` row), `docs/internals/traps-ledger.md` (R06.11 rows). Wiring left for the lead
(P15 core files, not edited): the registry row and credential table entry above; the daemon has to
hand `adapter.fetch_media` to `extras.intake.register_fetcher` (`adapter.register_fetchers(intake)`),
set `adapter.lost_handler` to take the platform down for the reconnect watcher, and call
`processing_started` / `processing_finished` if processing reactions are wanted; P16-32 registers
`split_target` as the `matrix` target splitter (the router reads `#…` as a directory name today, so
aliases need its parser). End-to-end encryption: P16-07 implements `create_end_to_end` in
`matrix/crypto.py` against the `EndToEnd` protocol in `adapter.py`.

Landed (integrator, 2026-10-20; D-16.384, D-16.385, D-16.387..393): `lost_handler` removed — a
token refused while syncing calls `report_fatal("matrix_auth", …)`; `register_fetchers(seat)`
returns the undo (the daemon's `ExtrasPart` calls it); `send_only` from `AdapterConfig.send_only`
keeps the sync loop off even with a sink; registry row (no extra; install hint says so) and
`BUILTIN_CREDENTIALS["matrix"]` added; `split_target` stays unregistered until P16-32 (open seam);
processing reactions are not called by the runner (open seam, as for Telegram); the contract case
moved to `tests/contract/gateway/matrix_case.py`.
