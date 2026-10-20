# Task: Photon adapter and sidecar (P16-25) Phase: 16 Owner: wave-28 builder E15

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/photon/adapter.py`
(whole file: lifecycle, inbound stream and dispatch, placeholder wait, rich-link preview
suppression, sidecar start / stop / reap, presence watchdog, outbound calls, standalone send),
`plugins/platforms/photon/README.md`, `plugins/platforms/photon/plugin.yaml`;
`plugins/platforms/photon/sidecar/` listed only (file names); the licence fields of
`plugins/platforms/photon/sidecar/package-lock.json` (package metadata only, for the licence check).
Evidence: none.

Vendor API: Photon Spectrum, reached through our own provider interface (`SpectrumLink`,
`apps/photon-sidecar/src/link.ts`). **No Spectrum client ships** (see "Licence check").

## Licence check (the gate in the P16-25 row)

- `spectrum-ts` is not available offline: it is in neither the uv cache, the pnpm store nor the npm
  cache on this machine, so nothing can be installed or inspected beyond package metadata.
- Package metadata seen offline (the reference's lock file at the pin): `spectrum-ts` 12.7.0 and its
  six `@spectrum-ts/*` packages declare `MIT`; across the 110 locked packages the declared licences
  are MIT (59), Apache-2.0 (31), BSD-2-Clause (10), ISC (5), BSD-3-Clause (1),
  `(Apache-2.0 AND BSD-3-Clause)` (1) and **BlueOak-1.0.0 (1)**, which is not on
  `tooling/checks/licenses_allow.json`. The reference also patches the SDK's compiled output at
  install time (a `postinstall` script), which our "no install scripts" pnpm rule refuses.
- So, as the brief says: the sidecar is built against our own provider interface (like the WhatsApp
  bridge), no Spectrum package is a dependency, and the adapter ships **disabled**: it is not turned on
  by the Photon credentials alone, and without an operator-supplied provider it refuses to start
  (`photon_provider_missing`, not retried). Intended difference for a D-id (see the lead section).

## What it does (own words)

Photon is a managed service that owns iMessage lines and hands their traffic to a TypeScript SDK
(Spectrum) over a long-lived stream. The gateway runs a small Node program of ours (the sidecar) that
holds that stream and offers it to the Python adapter over loopback HTTP: an at-least-once event
stream in, send calls out. The adapter owns the sidecar process (start, adopt, restart, stop) and turns
its events into gateway events.

## Sidecar (`apps/photon-sidecar`, `@indus/photon-sidecar`)

Settings from the environment only (never argv; the adapter builds the env with
`safety.child_env(variant="minimal")`, R02.22):

- `INDUS_PHOTON_TOKEN` (≥ 32 characters, fresh for every start): `Authorization: Bearer <token>` on
  every request, compared in constant time (SHA-256 of both sides, `timingSafeEqual`).
- `INDUS_PHOTON_HOST` (default `127.0.0.1`; only `127.0.0.1`, `::1`, `localhost`; anything else exits 2
  before binding), `INDUS_PHOTON_PORT` (default 8789).
- `INDUS_PHOTON_PROFILE` (reported by `/health`; the adapter refuses another profile's sidecar),
  `INDUS_PHOTON_PARENT_PID` (exit within 2 s once that process is gone).
- `INDUS_PHOTON_PROVIDER`: the operator's provider module (absolute path or package name) exporting
  `createSpectrumLink`. Missing: one `{"event":"error","error":"no_provider"}` line, exit 2, nothing
  bound; unloadable: `"provider"`, exit 2.
- `PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET` (vendor names, D-16.22): handed to the provider as
  options; never printed. Missing: `"credentials"`, exit 2.
- The provider's link is opened after the server listens (so `/health` answers `connecting` at once),
  retried 1 s doubling to 30 s; a refused login (`auth_failed`) stops the retries and is reported by
  `/health`. The provider owns the upstream stream's own reconnects.
- stdout: one JSON object per line, lifecycle only (`listening`, `link` state changes, `error`); no
  message text, no number, no secret.

HTTP API (JSON; `Host` must be loopback, token checked, then the route; bodies over 64 KiB are 413
before parsing; unknown route 404, wrong method 405):

- `GET /health` → `{status, profile, epoch, version, kept, link: {state, since, detail}}`; `status` is
  `starting | connecting | open | degraded | auth_failed | closed`.
- `GET /events?after=<seq>&epoch=<epoch>` → `application/x-ndjson`, one event per line: first every
  kept event with `seq > after` (all kept events when `epoch` names another run), then live events as
  they come; an empty line every 15 s as a heartbeat. At-least-once: a reconnect may repeat events and
  the provider itself may redeliver, so the adapter dedupes by id. The ring keeps the last 1,000 events.
- `GET /media?ref=<24 hex>` → the bytes of one inbound attachment (refs kept for the last 500, one
  hour); over 100 MiB is 413.
- `POST /send {space, text, format: "markdown"|"plain"}`, `POST /send-link {space, url}`,
  `POST /send-file {space, path, voice?, name?, mime?}` → `{id}`; `path` absolute, a regular file (not
  a symlink), ≤ 100 MiB.
- `POST /typing {space, on}`, `POST /react {space, id, emoji}`, `POST /unreact {space, id}`,
  `POST /read {space, id}` → `{ok: true}`.
- Errors `{error: <code>}`: 400 `bad_host` / `bad_request`, 401 `unauthorized`, 403 `forbidden` /
  `target_not_allowed` (a shared line may not start a conversation), 404 `space_not_found` /
  `message_not_found`, 413 `too_large`, 429 `rate_limited` (+ `Retry-After`), 502 `failed`,
  403 `auth_failed` (Photon refused the project credentials), 503 `not_connected` (+ `Retry-After: 5`
  while connecting), 504 `timeout` (maybe sent; never resent).

Event shape (ours): `{seq, id, kind: "message"|"reaction", space: {id, type: "dm"|"group", name},
sender: {id, name}, at (ms), text, media: [{ref, kind: image|video|audio|voice|file, mime, name,
size}], link: {url, title, summary} | null, reply: {id, text, fromUs} | null, reaction: {target,
emoji, targetFromUs} | null}`. The sidecar drops our own echoes (`fromUs`) and read receipts.

## Adapter (`src/indus/gateway/adapters/photon/`)

Capability flags: `max_len` 8,000 code points (`splits_natively`), `markup` markdown,
`supports_edit` false (streaming falls back to segments), `supports_delete` false,
`supports_buttons` false (approvals are the text card), `owns_access_policy` false (the core's
allowlist / pairing, `INDUS_PHOTON_ALLOWED_USERS`), `auth_upstream` false, `dm_is_private` true,
`push_capable` true (a shared line may refuse new targets: `forbidden`, final).

Lifecycle (`sidecar.PhotonSidecar`, state in `<home>/platforms/photon/<profile id>/`, 0700:
`sidecar.pid` JSON with pid + kernel start time + port + profile, `sidecar.token` 0600,
`sidecar.log` 0600):

- connect: credentials (`PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET`, secret store then env) and a
  provider (`platforms.photon.provider` / `INDUS_PHOTON_PROVIDER`) are required, else fatal
  `photon_credentials_missing` / `photon_provider_missing`; dependencies of the provider's package
  are healed first (`npm ci --ignore-scripts`, bounded to 600 s; see below); then adopt this
  profile's running sidecar (pidfile identity, healthy, our profile) or start one and wait up to 15 s
  for `/health`; anything else holding the port is never adopted or signalled
  (`photon_sidecar_port_taken`, fatal).
- `status` `auth_failed` → fatal `photon_auth` (credentials refused upstream).
- inbound: one long-lived `GET /events` stream with the cursor; a dropped or stalled stream (no line,
  heartbeats included, for 60 s) is reopened after 1 s doubling to 30 s; a dead sidecar is started
  again after 2 s doubling to 60 s (reset after 60 s up); `/health` every 30 s: `degraded` twice in a
  row, or three health checks timing out (a hung process), restarts the sidecar (a live process with
  a dead upstream stream is a silent outage).
- `indus send` (`send_only`): the running sidecar of this profile or nothing
  (`photon_sidecar_not_running`); never spawns, never reads the stream.

Dependency self-heal: our sidecar has no dependencies; the provider usually has (the SDK). When the
provider is a path, its package folder is the nearest folder above it with a `package.json` (at most 6
levels). If that folder has a lock file and `node_modules` is missing or older than the lock file,
`npm ci --ignore-scripts --no-audit --no-fund` runs there (child env minimal) with a 600 s bound; a
timeout kills it and is retryable (`photon_deps_timeout`), a failure is fatal (`photon_deps_failed`).

Inbound rules (one event → at most one `InboundEvent`):

- dedupe by event id: ≤ 4,000 ids for 48 h (`_http.SeenIds`); a replayed event does nothing;
- placeholder: text that is only U+FFFC (object replacement) holds the chat for up to 15 s for the real
  attachment; when it comes, one event (the attachment, the held words as its text,
  `metadata["placeholder_id"]`); after 15 s without it, the held words alone, or nothing (a log line);
- rich-link previews: an event made only of preview-art attachments (`.pluginPayloadAttachment` in the
  name or id) is dropped within 30 s after a link in that chat, ours (`/send-link` or a sent URL) or
  theirs;
- reactions: dropped unless `platforms.photon.reactions` is on; then only on our own messages, as
  text `reaction:<emoji>` replying to ours;
- groups: with `require_mention` (default off) a group message passes only for a slash command or a
  `mention_patterns` match (default: `indus` / `indus agent` as a word); the leading wake word is
  removed;
- media: one `Attachment` per item, ref `photon-media:<ref>#<name>` fetched lazily through the
  registered fetcher (`GET /media`, 100 MiB cap); kinds voice / audio / photo / video / document; an
  item over 100 MiB becomes a text note naming it (the agent still knows something arrived);
- replies: `reply_to_id`, `reply_to_text`, `reply_to_is_own` (from the event, else from our memory of
  the last 1,000 sends and the start of their text);
- a DM without a sender id is from its space (the one other person); a group message without one is
  dropped;
- a rich link becomes its title, summary and URL as text.

Outbound: `/send` per piece of ≤ 8,000 code points (markdown, except a piece with a URL goes plain,
markers stripped; `meta["plain"]` plain); a message that is exactly one URL goes by `/send-link` and
falls back to plain text when that is refused (never after a maybe-delivered failure, R06.4, and never
after a final one); `/send-file` per file (voice notes as voice), the caption as
a following text bubble; `/typing` on/off; `react` / `unreact`; `/read` after an admitted message when
`read_receipts` is on (default on). Failures: connect errors retryable, read timeouts and 504
maybe-delivered (never resent), 429 with `Retry-After`, 404 `space_not_found` chat-level,
`message_not_found` sub-chat, 403 forbidden (`target_not_allowed` and `auth_failed` final: no retry,
no plain-text resend), 400 bad format, 413 too long, 503 transient.

Logs: phone numbers masked (`mask_numbers`), the token and both credentials scrubbed, message text
never logged.

Edge cases / failure modes:
- no Node / no sidecar script → fatal `photon_node_missing` / `photon_sidecar_missing`;
- the sidecar exits at start (exit 2) → its first stdout error decides the code (`no_provider`,
  `credentials`, `bad_*` → fatal), anything else retryable `photon_sidecar_exited`;
- a recycled pid in the pidfile (other start time) → removed, never signalled;
- a stream that ends cleanly is reopened at once with the same cursor; a new epoch restarts it;
- a malformed event line is skipped (its type logged, never its text).

Our design differences / decisions:
- our env names (`INDUS_PHOTON_*`) and routes; Bearer token on every route plus a loopback `Host`
  allow-list; cursor + epoch on the event stream instead of a stream that starts at "now";
- media fetched lazily through `/media` instead of base64 inside every event (nothing is read for a
  message the gateway refuses; no 20 MB inline cap in the stream);
- the adapter never kills a process it cannot prove is its own (the reference reaped any process
  whose command line looked like a sidecar);
- no typing cooldown in the adapter (the core's typing loop paces refreshes; the contract suite counts
  every refresh);
- tapbacks reach the agent only with `reactions` on (the reference forwarded every tapback on ours);
- held words of a placeholder are kept and merged (the reference only handled a bare placeholder);
- previews are also suppressed after our own link sends (the P16-25 row);
- `npm ci --ignore-scripts` (no install scripts, like our pnpm rule), not `npm install` as a fallback;
- not taken: native polls for clarify prompts, message effects, the presence probe endpoint (the
  health poll covers it), the `photon` setup CLI (device login, project creation: owner decision),
  SDK telemetry switch, the out-of-process runtime record for cron (our `indus send` attaches through
  the pidfile + token file instead).

Tests we will write:
- vitest (`apps/photon-sidecar`): settings refused (token, host, port, provider, credentials) with exit
  2 before binding; no token / wrong token 401 (compare spied) and wrong Host 400; oversize body 413;
  event stream replays after the cursor, restarts on another epoch, streams live events and heartbeats;
  echoes and receipts dropped; sends, files (path checks), typing, reactions, read forwarded; error
  mapping; parent death exits within 2 s;
- Python: the shared contract suite over `tests/fakes/platform/photon_wire.py`; replayed event
  ignored; placeholder then attachment within 15 s merged (and the 15 s expiry); preview art suppressed
  for 30 s after our link send; sidecar token required (401 → port taken); restart back-off on the fake
  clock; degraded upstream restarts the sidecar; send-only attaches and never spawns; spawn env minimal
  and token not in argv; npm self-heal bounded (fake runner, fake clock); provider/credentials missing;
  numbers masked; the real Node sidecar from Python (skipped without Node).

## Decisions for the lead

- **Spectrum licence / availability → adapter shipped disabled (D-id wanted)**: no Spectrum package
  ships; the provider is operator-supplied through `SpectrumLink`; `platforms.photon.enabled: true`
  plus a provider are both needed. Record as an intended difference beside D-16.531 (WhatsApp).
- Registry row, credentials, target rule, setup step: see "Wiring for the lead" in the report and
  below. Not edited here (`registry.py`, `config.py`, `targets.py`, `gateway_setup.py` are core).
- Port: 8789 for every profile unless `platforms.photon.sidecar_port` is set; a second profile on the
  same port gets the fatal `photon_sidecar_port_taken` (never adopts or kills the first one's sidecar).

Wiring for the lead (proposed, not applied):
- `BUILTIN_NAMES`: `photon` after `bluebubbles`.
- Registry row: `PlatformEntry("photon", "iMessage (Photon)", factory=_lazy_factory(
  "indus.gateway.adapters.photon:create_adapter"), check_fn=modules_installed("httpx"),
  required_env=("PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET"), install_hint="install Node.js 24 and
  a Photon Spectrum provider module (exports createSpectrumLink; platforms.photon.provider), then set
  platforms.photon.enabled: true", allowlist/allow_all/allow_bots/home policy env names, max_len=8000,
  dm_is_private=True, platform_hint=<see photon/__init__.PLATFORM_HINT>, lock_env=("PHOTON_PROJECT_ID",))`.
- `BUILTIN_CREDENTIALS`: **no row while disabled** (a row would turn the platform on from env alone);
  the adapter reads both names through the secret store and scrubs them. Once Spectrum is approved:
  `"photon": ("PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET")`.
- Target rule (`gateway/adapters/targets.py`): `photon` — E.164 `+<6–15 digits>`, DM chat ids
  `any;-;+<digits>`, group ids `any;+;<id>` (letters, digits, `-_.`), email handles; anything else
  refused.
- Setup step (`cli/wizards/gateway_setup.py`): asks `PHOTON_PROJECT_ID` + `PHOTON_PROJECT_SECRET`
  (secret store), `platforms.photon.provider`, and sets `platforms.photon.enabled: true`; the check is
  the adapter's connect.
- Contract conftest: one import line (`photon_case`), done here (shared).
- `indus send` case for `delivery/test_send_p16_platforms.py`: `create_adapter(config,
  launcher=FakeLauncher(sidecar), transport=sidecar.transport)` attaches only.
- Doctor (P16-36): port 8789 (Photon sidecar).

Compare checklist (filled after the rewrite; reopened `plugins/platforms/photon/adapter.py` and
`README.md`):
- [x] at-least-once stream deduped by message id, 4,000 ids / 48 h (tests: `test_adapter.py::
  test_a_replayed_event_is_ignored`, `::test_dedupe_keeps_4000_ids_for_48_hours`)
- [x] stream drop reconnect 1 s doubling to 30 s (test: `::test_a_dropped_stream_is_reopened_after_1_s_doubling`);
  NDJSON split on LF only (tests: `::test_ndjson_lines_split_on_lf_only_and_drop_an_oversized_line`,
  `::test_a_new_sidecar_run_restarts_the_cursor_and_lines_split_on_lf_only`)
- [x] degraded upstream stream turns into a reconnect (test: `::test_a_degraded_upstream_link_restarts_the_sidecar`)
- [x] gap found: the reference's watchdog respawned a sidecar whose HTTP loop hung (consecutive hung
  probes) — fixed by counting health-check timeouts, three restart it, pinned by
  `::test_a_sidecar_that_stops_answering_its_health_check_is_restarted`
- [x] U+FFFC placeholder waits 15 s for the attachment (tests: `::test_a_placeholder_then_its_attachment_within_15_s_is_one_event`,
  `::test_a_placeholder_without_its_attachment_gives_its_words_after_15_s`)
- [x] rich-link preview art suppressed for 30 s (tests: `::test_preview_art_is_dropped_for_30_s_after_our_own_link`,
  `::test_preview_art_after_their_link_is_dropped_but_real_photos_are_not`); rich link → title,
  summary, URL text
- [x] reactions only on our messages, read receipts not turned into turns (sidecar drops them), read
  receipts sent after forwarding (tests: `::test_tapbacks_reach_the_agent_only_when_asked_and_only_on_ours`,
  `::test_replies_media_and_read_receipts`; `http.test.ts` "drops our own echoes and read receipts")
- [x] gap found: a reply to one of our messages whose text the platform did not hydrate quoted
  nothing — fixed with the sent-text memory, pinned by `::test_a_reply_to_our_message_quotes_what_we_sent`
- [x] gap found: an attachment over the cap vanished silently when the message had no text (the
  reference left a marker) — fixed with a text note, pinned by
  `::test_an_attachment_too_large_to_fetch_is_named_in_the_text`
- [x] gap found: sender id fallback to the space for a DM — pinned by
  `::test_a_direct_message_without_a_sender_is_from_its_space`
- [x] group wake words strip the leading wake word only (test: `::test_group_messages_need_the_wake_word_when_asked`)
- [x] markdown kept unless the piece has a URL (stripped then); `markdown: false` strips; URL-only →
  rich link with a plain fallback (tests: `::test_markdown_goes_as_markdown_and_a_piece_with_a_url_as_plain_text`,
  `::test_markdown_can_be_turned_off`, `::test_a_refused_link_send_falls_back_to_plain_text_but_not_a_maybe_sent_or_refused_target`)
- [x] `target_not_allowed` / refused credentials final: no retry, no plain resend (same test)
- [x] files by path, voice notes as voice, the caption as a following bubble (test: `::test_files_go_by_path_with_the_caption_after_them`)
- [x] sidecar start: Node missing / dependencies missing are fatal, the install bounded to 600 s,
  stale install (lock newer) reinstalled, 15 s readiness wait (tests: `test_sidecar.py::
  test_missing_settings_fail_for_good_before_anything_runs`, `::test_npm_ci_heals_a_missing_install_without_scripts_bounded_to_600_s`,
  `::test_needs_install_follows_the_lock_file`, `::test_a_sidecar_that_never_answers_is_stopped_and_retried_later`,
  `::test_run_npm_stops_a_command_at_its_bound`)
- [x] stop: TERM, wait, KILL; pidfile + token removed (tests: `test_sidecar.py::test_a_restarted_adapter_adopts_its_own_running_sidecar`,
  `test_real_sidecar.py::test_the_adapter_runs_the_real_sidecar`)
- [x] sidecar crash → restart (tests: `test_adapter.py::test_a_dead_sidecar_is_restarted_after_2_s_doubling`)
- [x] out-of-process sends reach the live sidecar only, never spawn (tests:
  `::test_send_only_uses_the_running_sidecar_and_never_streams`, `::test_send_only_without_a_running_sidecar_fails_and_spawns_nothing`)
- [x] loopback only and a per-start token (tests: `::test_the_sidecar_token_is_required`,
  `::test_a_token_refused_mid_run_is_fatal`; `http.test.ts` "refuses a request without the token…",
  "refuses a Host…", "binds loopback only"; `main.test.ts` "exits 2 on a non-loopback host…")
- Deliberate differences: see "Our design differences / decisions" above; also a 502 from the
  sidecar on a link send is "maybe delivered" and not re-sent as text (the reference re-sent on any
  rich-link failure); provider overload is the provider's to report as `not_connected` (503, retried)
  rather than our adapter matching overload phrases in error text; the sent-text memory is in memory
  only (the reference persisted it across restarts).
- Not taken: processing tapbacks (👀/👍/👎) — the runner does not call processing reactions for any
  adapter yet (`react` / `unreact` are ready); the agent-facing "react to the latest inbound" default;
  native polls, effects, chat-info lookups, telemetry, the `photon` setup CLI (device login, project
  and user provisioning), the SDK patch script; PII redaction of E.164 ids in session descriptions (a
  core concern, not adapter code).

## Status

Done (2026-10-20), shipped disabled (no Spectrum client; provider operator-supplied). New:
`apps/photon-sidecar/` (package, sources, vitest), `src/indus/gateway/adapters/photon/`
(`__init__.py`, `adapter.py`, `sidecar.py`), `tests/fakes/platform/photon_wire.py`,
`tests/contract/gateway/photon_case.py`, `tests/unit/gateway/adapters/photon/`. Shared: one import
line in `tests/contract/gateway/conftest.py`; the `platforms.photon` row in
`foundation/config/schema.py` and its pin in `tests/unit/foundation/config/test_schema.py`; trap R02.22
refs; ACC-K12-2. Registry row, target rule and setup step are for the lead ("Wiring for the lead").

Integration (2026-10-20, P16 integrator): wired as proposed, still shipped disabled — `BUILTIN_NAMES`
after `bluebubbles`, the registry row (no `BUILTIN_CREDENTIALS` row), the target rule (E.164, email,
`any;-;<handle>`, `any;+;<id>`), `indus gateway setup photon` (two credentials, the provider, then
`enabled: true`), an `indus send` case over the fake sidecar, doctor rows and port 8789; the explicit
contract import line was dropped (cases are discovered). D-16.564 – D-16.567.
