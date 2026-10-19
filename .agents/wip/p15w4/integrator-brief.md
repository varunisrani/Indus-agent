# Integrate wave 22 (P15-33 Telegram, P15-34 Discord, P15-35 Slack) and build P15-36 Demo 3

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify4 (from main; base of the three
patches is 78be7b3). Do NOT commit. The lead commits and lands. Never write under the real ~ or /root. No network.
Wave 23 builders (P16 E1-E3, P17 K1-K2) run in parallel from 78be7b3 — do not touch their areas
(src/indus/gateway/adapters/<other platforms>, P17 service/state modules).

Patches (scratchpad/patches/): w22-p15t.patch (Telegram), w22-p15d.patch (Discord), w22-p15s.patch (Slack).
Read .agents/wip/p15w4/README.md and .agents/wip/p15w4/brief.md in full first, plus each builder's task note in
docs/notes/P15/, docs/plan/phases/P15-gateway-core-demo3.md (rows 33–36, Acceptance, Done when),
docs/plan/phases/LEAD-NOTES.md "P15 waves 2–3 landing notes (lead)", AGENTS.md, HANDOFF.md.

LEAD RULINGS (seams differ between builders):
- Send-only: ONE shape = `AdapterConfig.send_only: bool = False` (S and D). Telegram's `BaseAdapter.connect(...,
  send_only=...)` kwarg goes away; Telegram reads `config.send_only`. send_command.build_adapter sets it. Each
  adapter keeps a test that `indus send` does no inbound work.
- Media fetchers: ONE shape = adapter method `register_fetchers(intake) -> undo` (S and D). Telegram's
  `fetch_media` becomes its `register_fetchers`. Keep Telegram's daemon-side plumbing (`extras.attach_fetcher()`
  in ExtrasPart on platform up/down) but have it call `adapter.register_fetchers(intake)` and the returned undo.
  Test up → registered, down → removed, for all three.
- Telegram webhook listener: default bind 127.0.0.1 (public bind only when `webhook_host` is set explicitly),
  matching the P16-01 kit rule and D-16.289. Record as a D-id.
- Telegram revoked token (`polling_stopped == "auth"`) must surface as a fatal platform state in the daemon
  (status shows it, no retry loop) if the daemon's platform-state seam supports it; else record an open seam.

Steps:
1. `git apply --3way` S, D, T. Resolve as unions. ONE `AdapterConfig.send_only` field and one send_command change
   (S and D both add it; reconcile T's shape onto the same field). One import line per case in
   tests/contract/gateway/conftest.py. THIRD_PARTY_NOTICES aiohttp row names both extras once.
2. Re-lock: `uv lock` (exclude-newer unchanged). Expect only slack-bolt 1.30.0, slack-sdk 3.44.1, discord-py 2.7.1,
   audioop-lts 0.2.2 (+ whatever T reports, ideally nothing). Report every new package + licence; flag anything
   not on licenses_allow.json. Pin rows in docs/plan/phases/00-layout-and-conventions.md §4 for each.
3. Wiring:
   - Media fetchers: the daemon calls `adapter.register_fetchers(extras.intake)` when a platform comes up and calls
     the returned undo when it goes down (ExtrasPart via daemon.on_platform_up/down or the equivalent seam). Test it.
   - Slack outbound-thread seam: a top-level channel message keys its session on its own ts (prospective_thread_id)
     but the core takes the outbound thread only from `source.thread_id` → 2nd+ chunks and approval/clarify cards
     go top-level. Fix in `replies.final_reply_for` / `prompts._meta` (per-adapter capability or flag, so Discord's
     meaning is unchanged) with tests on both platforms.
   - Typing: pass `{"thread_id": thread_id or prospective_thread_id}` metadata where the typing port is called (if the
     runner does not yet feed TypingLoop, record as an open seam, don't build the feed).
   - Discord `extra_commands`: pass plugin/skill command names if the daemon already has them; else open seam.
4. Build P15-36 Demo 3 exactly per its row: tests/e2e/gateway/test_demo3.py (fake model + the three wire fakes in one
   daemon process: same agent answers on all three; approval granted by a Telegram button; SIGKILL between ledger
   `attempting` and ack, restart, one labelled recovered reply; cron "every 30m" delivers to a Telegram chat at the
   right fake-clock tick; chunker, busy guards and default-deny on each platform) and tests/e2e/gateway/live_demo3.py
   (`network` marker, needs INDUS_LIVE=1 + real tokens, never in default CI; tokens only from env/SecretStore,
   never printed). Map the four M5 acceptance checks in docs/internals/acceptance-map.md.
5. Security review of the combined diff: tokens never in logs/exception text/reprs/argv/URLs that get logged
   (Telegram puts the token in the URL path — check every httpx error path and log line); media fetchers send
   credentials only to the vendor's own https hosts; callbacks/presses from unauthorized users refused; webhook mode
   (Telegram) requires the secret token; inbound content treated as untrusted.
6. D-ids: number this wave's decisions D-16.330 onward in docs/plan/16-what-we-change.md §2.7 (+ §3.1 row). Add a
   "P15 wave 22 + Demo 3 landing notes (integrator)" section to LEAD-NOTES.md. Update acceptance/trap rows
   (ACC-K13-1 per-platform dedupe line).
7. No `# STAND-IN` first line anywhere in src/ tests/.
8. Full gate, all green, with NO extras installed (`uv sync --locked`, as CI):
   uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
   INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
   uv run pytest -q -p no:cacheprovider -m perf     # alone, after
   uv run python -m tooling.checks --require-ref
   CI=true pnpm test --maxWorkers=1
   Also run the three adapters' drift checks once with their extras installed (`uv sync --locked --extra slack
   --extra discord`), then return to no extras before the final full run.
   A failure that passes alone must pass alone 3x AND be explained; never skip or mark tests.
9. Credential-literal scan of the diff (Telegram [0-9]{8,10}:[A-Za-z0-9_-]{35}, Discord token shape, xox[bape]-),
   no stray processes or sockets.
10. Usage: the lead may message you to wrap up; then stop, reach a consistent state, report.
Report: files changed beyond the patches, wiring done, Demo 3 results, D-ids, licences, security fixes, gate counts,
open seams / follow-ups.
