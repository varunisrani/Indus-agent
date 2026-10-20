Wave 24 continues P16 from main (base given in your prompt). Since wave 23's brief (below, still binding) more has
landed: P15-33..36 (Telegram/Discord/Slack adapters, Demo 3), P16-01 HTTP listener kit + identity helpers
(src/indus/gateway/adapters/_http.py, _identity.py; how-to in docs/notes/P16/http-kit.md; template callback adapter
tests/unit/gateway/adapters/http_sample.py with tests/fakes/platform/http_wire.py), P17-01..07. P16-05/06/08/09
(Signal, Matrix, Mattermost, Email) are built but parked (not on main) — do not touch those folders.
Adapter seams now on main (use exactly these): send-only = `AdapterConfig.send_only` (no inbound consumption when
true); media = `Adapter.register_fetchers(seat) -> undo | None` (ExtrasPart registers on platform up/down);
fatal platform state = base `report_fatal` (daemon disconnects fatal, no retry); `Capabilities.reply_opens_thread`.
Read LEAD-NOTES "P15 wave 22 + Demo 3 landing notes" and "P16-01 landing notes (lead)".
Registry rows / BUILTIN_CREDENTIALS in P15 core: do NOT edit; put the exact rows you need in your report.

Slices this wave:
- E4 = P16-04 WhatsApp Business Cloud adapter + P16-10 SMS (Twilio) adapter (both on the kit).
- E5 = P16-23 LINE adapter (on the kit) + P16-26 ntfy adapter + P16-27 IRC adapter.

--- wave 23 brief (rules still apply; its slice list is history) ---
Wave 23 starts P16 (more platforms) and P17 (daemon as shared backend) from main 78be7b3, in parallel with wave 22
(P15-33 Telegram, P15-34 Discord, P15-35 Slack adapters, being built now — never touch
src/indus/gateway/adapters/{telegram,discord,slack}/ or their wire fakes).
Landed: P00–P14, P15-01..32 (gateway core, adapter contract + fake platform harness, registry, config/AdapterConfig,
sessions, auth, formatting, chunker, busy guards, delivery ledger/router/`indus send`, cron in the daemon,
approvals/clarify over chat, media intake + register_fetcher, daemon parts, control socket, `indus gateway`,
API server, webhooks), P21, P22.
Read in full first: your phase file (docs/plan/phases/P16-more-platforms.md or P17-daemon-shared-backend.md —
your rows, Files to create/update, Fixtures, Traps, Acceptance, Run until green), AGENTS.md, HANDOFF.md,
docs/plan/phases/LEAD-NOTES.md ("P15 wave 1 …", "P15 waves 2–3 landing notes (lead)" incl. open seams),
docs/plan/16-what-we-change.md §2.7 rows for P15, and the code your rows build on.

Slices (each owns only its tasks' files):
- E1 = P16-01 inbound HTTP listener kit and identity helpers (the kit later HTTP adapters use).
- E2 = P16-06 Matrix adapter (plain; E2E is P16-07, later) + P16-08 Mattermost adapter.
- E3 = P16-05 Signal adapter + P16-09 Email adapter.
- K1 = P17-01 runtime ledger (store), P17-02 admission service, P17-03 durable turn lease, P17-04 turn liveness watchdog.
- K2 = P17-05 generation fences on controls, P17-06 event log and replay, P17-07 durable server-request re-delivery.
  K2 builds on K1's store (built in parallel): stand in only what you need under tests/.../_stubs_k2.py with
  FIRST LINE "# STAND-IN: removed at integration" (never in src/), and state the interface you assumed.
Cross-slice stand-ins follow the same rule (_stubs_<slice>.py). No stand-ins in src/.

Rules:
- P16: each adapter owns only its adapter folder, its wire fake and its tests, plus its own pyproject extra row and
  THIRD_PARTY_NOTICES rows. No P16 task edits a P15 core module: if you need a core change, STOP that part and
  report it to the lead with the exact change proposed. Each adapter implements `create_adapter(AdapterConfig)`,
  a send-only open for `indus send` (no inbound consumption), media fetchers via extras.intake.register_fetcher
  where the platform has media, joins the shared contract suite (tests/contract/gateway conftest parametrisation),
  and passes its Verify column. Check how the registry rows name your platform (src/indus/gateway/registry.py).
- Third-party libraries: optional extras with exact pins (uploaded ≥14 days before exclude-newer 2026-10-04),
  imported lazily, pyright-ignored; the contract suite must run your adapter over its wire fake WITHOUT the SDK
  installed. Licence must be on licenses_allow.json (MIT, BSD, ISC, Apache-2.0, PSF, MPL-2.0 …) — LGPL/GPL/AGPL
  are NOT allowed: if the natural library is copyleft, write our own minimal client over httpx/asyncio stdlib
  instead and record the decision. List every new package (incl. transitives you can see) with its licence.
  Leave uv.lock to the lead (`uv run --frozen` if needed).
- Credentials only from AdapterConfig / SecretStore (vendor env names per D-16.22); never in logs, exception
  text, reprs or argv. No credential-shaped literals anywhere (build fake tokens at runtime).
- No real network (socket guard stays on), never write under the real ~ or /root, no stray processes/sockets.
- Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/<phase>/<slug>.md; Read the reference paths in your row, close them,
  write; Compare after; tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code,
  comments or docs.
- Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> tests/contract/gateway -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report
done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, licences,
follow-ups.
