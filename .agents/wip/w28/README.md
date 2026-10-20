# Wave 28 parked patches (P17-23..27)

## w28-e15.patch: P16-25 Photon adapter and sidecar — done, ships DISABLED (base c84f1db)
- Builder checks: photon unit 65 passed, contract 11 checks; brief set 546 passed / 48 skipped; gateway/wizards/config/
  integration gateway 2474 passed; pnpm 957 passed (photon-sidecar 22); pnpm -r typecheck, biome, ruff, pyright,
  lint-imports, tooling.checks ok. No packages (Python or Node); both locks unchanged — integrator: confirm
  `pnpm install --frozen-lockfile --offline` accepts the new workspace package without a lock change.
- Licence: spectrum-ts not available offline; reference lock shows MIT for spectrum-ts but one transitive BlueOak-1.0.0
  (not allow-listed) and a postinstall patch (refused). Sidecar built on our own provider interface (SpectrumLink), like the
  WhatsApp bridge; operator-supplied provider. OWNER DECISION alongside Baileys.
- New: apps/photon-sidecar (package.json @indus/photon-sidecar, src main/http/link/relay, fakeLink, tests),
  gateway/adapters/photon (__init__, adapter, sidecar — imports PidRecord/Child/spawn_process/port_free from
  whatsapp/sidecar.py), tests/fakes/platform/photon_wire.py, contract photon_case.py, unit tests, docs/notes/P16/photon.md.
- Shared: contract conftest import; schema row platforms.photon + test; traps R02.22 refs; ACC-K12-2.
- Wiring (core, proposed): BUILTIN_NAMES += photon after bluebubbles; PlatformEntry("photon", "iMessage (Photon)", lazy
  indus.gateway.adapters.photon:create_adapter, check_fn=modules_installed("httpx"), required_env=("PHOTON_PROJECT_ID",
  "PHOTON_PROJECT_SECRET"), INDUS_PHOTON_* policy env, max_len=8000, dm_is_private=True, platform_hint=photon.PLATFORM_HINT,
  lock_env=("PHOTON_PROJECT_ID",), install_hint=<in report/note>); NO BUILTIN_CREDENTIALS row while disabled (a row would
  enable from env alone); target rule (+<6–15 digits>, any;-;+<digits>, any;+;<id>, email handles); setup step (two
  credentials to SecretStore + provider, enabled: true, connect as check); indus send case with FakeLauncher; doctor port
  8789; remove photon from E17's matrix pending allow-list.
- Decisions (D-ids): Spectrum not shipped / adapter disabled with operator provider; tapbacks only with
  platforms.photon.reactions; no own typing cooldown; never reap a port held by another process (8789 unless
  sidecar_port); lazy media; 502 on link send = maybe-delivered, no text resend; npm ci --ignore-scripts with no install
  fallback; reply-quote memory in-memory only.
- Slip: builder's pnpm fetched 85 packages from the network into its scratchpad store before being pinned offline (nothing
  under /root, no lock change). /root/.npm/_logs/2026-10-20T14_05_* came from another worktree (L3) — owner item.
- Follow-ups: owner decision on a Spectrum provider + live lane; processing tapbacks once the runner calls them; polls/
  effects, setup device login, E.164 redaction in session descriptions (core); sidecar packaging (P23/P25).

## w28-e17.patch: P16-35 Discord voice channels + P16-36 adapter matrix gate and doctor gateway checks — done (base c84f1db)
- Builder checks: voice 23, matrix 12, doctor 17 new tests; voice/doctor/contract/send 544 passed / 53 skipped;
  tests/unit/cli 1295 passed; gateway unit/integration/e2e 2210 passed; discord extra venv 63 + vendor-name drift 37
  passed; ruff, pyright, lint-imports, tooling.checks ok. No stand-ins. Real voice receive over discord.py's socket is
  untested (PyNaCl/davey/libopus/ffmpeg) — live lane.
- Extra `discord-voice` (discord.py[voice] 2.7.1 MIT; PyNaCl 1.5.0 Apache-2.0 bundling libsodium ISC, needs cffi MIT-0;
  davey MIT). Lead trial re-lock (online): davey 0.1.6 (uploaded 2026-06-22, PyPI classifier MIT), pynacl 1.5.0
  (2022-01-07) — integrator: re-lock with `uv lock`, fill davey's pin row (0.1.6) in 00-layout §4.
- New: discord/voice.py (+test), tests/contract/gateway/test_adapter_matrix.py, cli/doctor/gateway_checks.py (+test),
  notes discord-voice.md, adapter-matrix.md.
- Shared: discord/adapter.py (/voice join|leave|status; speak sends into a joined voice channel; leave before close;
  voice_states intent; picker hint; vendor_names), discord/__init__.py, tests/fakes/platform/discord_wire.py (voice
  states), tests/contract/gateway/conftest.py (case modules DISCOVERED by *_case.py, import failures recorded in
  CASE_IMPORT_ERRORS — E15's photon_case.py import line becomes redundant), cli/doctor/checks.py (registers section),
  tests/unit/cli/commands/test_doctor.py (gateway in order), .github/workflows/ci.yml (new `adapters` job in the gate's
  needs), pyproject.toml, THIRD_PARTY_NOTICES, 00-layout §4, ACC-K45-1 + ACC-K8-1 ref.
- Wiring: remove photon from PENDING in test_adapter_matrix.py when E15 lands.
- Decisions (D-ids): voice turns keyed on the voice channel; adapter answers /voice join|leave|status gated on allow-all/
  allowlist/role/pairing; 0.8 s silence gap per the row (reference 1.5 s); rejoin semantics, buffered speech dropped at
  leave, DAVE passthrough frames dropped; no ambient bed / spoken acks; voice_idle_seconds from the free-form platform
  block (no schema row); contract cases discovered; four non-chat surfaces (api_server, webhook, msgraph, raft) outside
  the chat suite; doctor reachability probes run on every `indus doctor`.
- Follow-ups: live voice lane; speak the final edit once the runner streams replies; live vendor lanes (owner).

## w28-e16.patch: P16-33 native draft streaming (adapter side) + P16-34 Telegram DM topics and stickers — done (base c84f1db)
- Builder checks: native streams 26 + topics 18 new tests; gateway adapters + contract + config 1894 passed / 58 skipped
  (earlier wider gateway unit/integration/contract 2821 passed); ruff, pyright, lint-imports, tooling.checks ok. No packages.
  Slack vendor-name rows skipped without the slack extra (stream calls checked by hand in slack-sdk 3.44.1).
- New: gateway/adapters/_native.py (NativeStream push/finish/abandon, flood back-off, display flag reader),
  telegram/drafts.py, slack/streams.py, telegram/topics.py, telegram/stickers.py, tests test_native_streams.py,
  telegram/test_topics.py, fixtures native_stream_frames/{telegram_drafts,slack_stream}.json, notes native-streams.md,
  telegram-topics.md.
- Shared: telegram/adapter.py (open_stream, drafts_unavailable, topics, sticker notes, register_fetchers, /topic menu, options
  dm_topics off / sticker_descriptions on / native_stream), telegram/__init__.py, telegram/bot_api.py (send_message_draft,
  create_forum_topic, ForumTopic, User.has_topics_enabled, sticker FileRef fields), slack/adapter.py (open_stream,
  vendor_names, native_stream); schema DisplaySection.platforms: dict[str, DisplayPlatformSection(native_stream=False)] +
  test; telegram/slack wire fakes; test_vendor_names (sendMessageDraft, createForumTopic); traps R06.12, R06.13; ACC-K24-3,
  ACC-K28-2, ACC-K46-1.
- CORE CHANGE OWED (P16-33 does nothing until applied): `w28-e16-core-proposal.patch` (297 lines; check script
  w28-e16-prop_check.py.txt; builder applied it, 213 tests + 2 e2e checks green, then reverted): Adapter.open_stream(chat_id,
  *, reply_to=None, meta=None) -> NativeStream | None (default None); ports.open_native_stream; StreamConsumer native branch
  (open on first interim, push raw redacted text to last whitespace; tool call finishes the segment unless the stream is the
  whole reply (Slack, WeCom); approval seals; final via stream.finish; failure falls back to edits; cancel abandons); WeCom
  open_stream(meta), WeComStream.one_message_per_turn = True + abandon(); ALSO make WeComStream.push return True for a
  non-extending frame. Runner still must feed the consumer (open since P15 wave 22); revisit streaming_default("wecom").
- Proposed core seams: sticker descriptions after authorization (intake hook, e.g. register_sticker_notes from enrich;
  today bounded: private chats / addressed group messages only, 10 fresh per sender per hour, cached); `/topic` as a
  registry command routed to adapter open_topic after authorization (today adapter-handled, DM only with dm_topics, 1 per
  10 s per chat, not behind the core auth chain).
- Decisions (D-ids): switch at display.platforms.<p>.native_stream (WeCom keeps platforms.wecom.native_stream); Telegram
  drafts plain text, final MarkdownV2 with plain fallback; Slack over 39,000 chars seals with what fits, remainder as thread
  messages (reference resends whole final); final after seal edits the sealed message; drafts off inside topics only if a
  topic refuses; no DM topic tables/renames/restore/reminder; sticker cache LRU 2,000.
- Follow-ups: live check of sendMessageDraft / has_topics_enabled / createForumTopic and Slack recipient fields; Slack
  vendor-name check with the extra; the two core seams.

P17-23..27 (L1..L3) LANDED as d1b05df; their sections and patches were pruned.
