# Lead notes: plan reconciliation (after all phase writers finish)
## From W2 (P04-P08)
- Glossary 17 §a1 says indus.agent; architecture says src/indus/core/ -> fix glossary.
- Add D-ids to 16 §2.7: explicit builtin tool list (P04-08); toolsets include registry tools (P04-11); no TERMINAL_* env bridge, terminal config from Scope (P05-04, supersedes INDUS_TERMINAL_* proposal); own approval pattern corpus (P05-07); bang shell refuses without guard (P06-11); auth.json metadata only, secrets in keychain (P07-06); backups skip keychain secrets + warn (P07-22); no `indus login` (P07-08).
- Decisions: device-code slow_down +5 s per RFC 8628 unless provider documents otherwise; summary ceiling 10,000 until REA (D-16.24); iteration cap 150 (D-16.62).
- Licences: ddgs, pypdf, python-docx, openpyxl -> allow-list check when added.
- Gaps placed: A42 aux client in P08-17; A52 route switch in P04-23.
- 9-gram hits reported in P09, P10, P18, P22, P23 (other writers) -> recheck at the end.
## From W6 (P21-P23, P25, build plan)
- Lead decision D-16.70: Windows desktop installer = signed NSIS (electron-builder) at 1.0; MSIX deferred (D-16.15). Add to 16 §2.7.
- S9 `indus pm` -> assign to P24 (add a task there or a §2.8 row if W5 didn't).
- O9 legacy single-query runner -> add §2.8 row.
- Windows compose: dashboard needs a login provider (no insecure flag, D-16.25) -> user docs.
- Owner-open: ai.indus.* app ids, GHCR owner, docs domain (P25-20 checklist).
- Build plan links to P02/P03/P13/P14/P20/P24 must resolve after all writers finish.
- 9-gram hits to recheck: P08, P09, P10, P18.
## From W4 (P12, P18-P20)
- Ruled: P17 uses tui_bridge/stdio_server.py + dispatch.py; ws auth helper = src/indus/service/ws_auth.py (P17 owns, P18 consumes -> fix P18-06 to consume, not create web/ws/auth.py).
- Slash registry must live below cli: move definitions to src/indus/service/commands/ (fix P06 tasks); P12-31 subprocess slash worker can then become in-process (keep as fallback note).
- Desktop REST: decision = P18 provides `indus serve` ensure/discover (headless, token minted by caller); desktop uses daemon WS for JSON-RPC + serve for REST. Fix P20-10 (drop per-request HTTP grant to daemon).
- P23 must implement the installer manifest/stage contract P20-09 drives (fix P23 task text).
- Unphased N/L features -> P24 (sent to W5).
- Add to 16 §2.7: no auto sandbox-disable on GPU crash; no bundled-payload resolver rung; acp.allow_local_fallback=false default; unsigned Windows build NSIS+ZIP (with D-16.70).
- Glossary fixes: tui_bridge (not tui_backend), @indus/client (not @indus/shared), ai.indus.desktop app id still owner-open.
## From W3 (P09-P11, P13, P14)
- Confirmed: curator SkillReferenceSource registry in P09-18; P14-03 registers cron store.
- P04 fix: ToolHooks.before fail-open only for the empty seam; plugin pre_tool_call hooks fail closed (R07.18) — P13-17 updates tools/hooks.py.
- Fill guessed paths: "(P06 path)" -> src/indus/service/commands/; "(P01 path)" -> src/indus/foundation/config/schema.py; "(P07-24 path)" -> from P07 file.
- P02 needs a per-session metadata API (goals/loops/heartbeat) -> add to P02 tasks.
- D-ids to add: D-16.71 TTS default = permissively licensed engine from allow-list; D-16.72 MoA turns exempt from byte-stable cache (R01.1 exception); LOOP_COMPLETE token kept (generic).
- Every-N-days compile rule: P14-01 note decides.
## From W1 (layout, P00-P03)
- `indus doctor --minimal` wiring -> P07 doctor task owns it (M0 demo uses python -m indus.state.doctor_report until then).
- P00-10 adds requires-python upper bound <3.15 (architecture).
- AGENTS.md link to 01-clean-room-protocol.md is fine (file exists since P00 landing).
- Pin window D-16.52 proposed but enforced by `pins` check -> mark D-16.52 accepted (lead).
- Node 24 LTS, TypeScript 6.0.3 -> record in architecture stack table.
- agent-client-protocol licence metadata missing -> P19 builder verifies LICENSE.
- Live captures may land synthetic first (D-04.5).
- Glossary module names -> follow architecture (foundation/home.py, log.py, clock.py).
## From W5 (P15-P17, P24) — lead rulings
- P18 must consume src/indus/service/ws_auth.py (P17-10) — remove its own web/ws/{auth,tickets}.py.
- indus.pm layer: add to P00-10 import-linter contract at the providers/safety level.
- Confirm (lead): INDUS_<PLATFORM>_ALLOWED_USERS-style policy vars; D-16.26 API server always needs a key; `@default` session-key namespace -> mark accepted in 16 §3.3 and glossary (e).
- K39 browser-control broker -> add a P24 task.
- §2.8 rows: Home Assistant adapter (external catalogue plugin), Teams meeting pipeline, non-English locales (D-16.51), cloud connection kind (D-16.12).
- Licences: signal-cli (GPL-3.0) and SimpleX run as separate processes only; Photon Spectrum, egress proxy binary, Porcupine gated on allow-list; WhatsApp bridge lib newest stable.
- P24 = 46 tasks ≈ 3.5 weeks: update 15-roadmap P24 sizing.
- P20 traps: R08.11 now owned by P24-07/P24-31. P23 uninstall flags table consumed by P24-34.
- Cross-phase file updates by P15-P17/P24 into P04/P05/P06/P07/P08/P12 files: confirm names exist in those phase files.
## Wave 2 landing notes
- env: add ERROR_CODES rows secrets.unavailable/backend_failed/unreadable/bad_mode; maybe move SecretStoreUnavailable to errors.py. Remove tests/unit/foundation/_stubs.py + install() lines. THIRD_PARTY_NOTICES rows for pydantic, ruamel.yaml. cffi MIT-0 exception added. Redaction skips public-looking values (_looks_public) -> record D-id.
- log: remove _stubs_log.py; bootstrap.make_root_scope adapt to real Scope/SystemClock/resolve_zone; _secure_mkdir -> home.secure_dir.
- hypothesis/pytest-asyncio not in dev group: add when within exclude-newer.
- core: error code naming clash: core has `secret_store.unavailable`, env uses `secrets.*` -> unify on `secrets.*` in ERROR_CODES. env must use core's EnvView (scope.py). profiles.py private atomic write -> fsio.atomic_write_text. conftest scope fixture secrets=None -> file secret store.
## Wave 3 landing notes
- P03-B1 stand-ins: tests/unit/providers/_stubs_p03a.py, tests/contract/providers/_stubs_p03b2.py -> replace with real RuntimeRoute/ProviderProfile and FakeModelServer.
- errors.py merges: provider.http/stream (B1) + provider.* six codes (A) + B2's.
- Both A and B1 create tests/unit/providers/__init__.py (and maybe conftest) -> merge.
- Accept A's behaviours: KeyHostMismatch for vendor on foreign host; OPENAI_BASE_URL only for custom; context_length.resolve async -> record D-ids.
- P06 bootstrap must call bundled.register_managed_env_names() early.
- Transport _retry_after_seconds -> P03-16 parse_retry_after.
- P02-A: host SQLite 3.45.1 is in the WAL-reset bug range -> stores open in DELETE mode here. Decision pending: uv-managed CPython with SQLite >=3.51.3 for dev/CI, and/or an explicit override setting. Wave 4 should know reads go through the writer in DELETE mode.
- ERROR_CODES missing: store.compression_busy, store.compression_closed, store.turn_lease_lost (TYPED_BUCKETS in state/errors.py).
- state/__init__.py stays docstring-only (layers test); public names in state.registry / state.writer.
## Wave 4 landing notes (Agent 2)
- Repository shape: module functions taking the handle first, plus `*_in(conn, …, now=…)` forms for use inside a bigger transaction (lead decision for wave 4, replaces "methods on the handle").
- Merge fixes: `sessions.create_in` also accepts `created_source` and the lineage flags (`archived`, `auto_archived`, `hidden`, `pinned`) that continuations and branches carry; an untitled parent's first branch is titled `branch` (a free base is returned as is), `#2` from the second on.
- `TYPED_BUCKETS` rows added: compression_parent_ended → compression_closed, session_guarded → compression, session_not_found → session_row_missing, vacuum_refused → locked.
- Accepted builder decisions: handoff rows marked `finish_reason = 'indus.handoff'`; appends refuse a compression-ended session (`store.compression_closed`), the lock is never checked; branch `model_config` = only `_branched_from`; tombstoned id in an import refuses the whole import; store without FTS5 searches with LIKE; doctor label keys `word_yes`/`word_no`.
- Open: `sessions.export_message_limit` is read but not in the config schema; FTS stale rebuild at open can wait up to 120 s on the cross-process lock; CI needs a `slow` lane for `tests/integration/state` (the stress test runs 60 s per mode by default; `INDUS_STRESS_SECONDS` shortens it).
- P02-21: D-04.6 verdict "stdlib sqlite3 holds" (DELETE ~310 appends/s, WAL-pinned ~630; numbers in `docs/notes/P02/stress-test.md`).
