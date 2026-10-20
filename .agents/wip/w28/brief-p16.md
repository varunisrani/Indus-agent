Wave 28 (P16 part) builds from main c84f1db. Landed: P00–P15, P21, P22; P16-01..24, 26..32 (31 adapters on the P16-01 kit:
registry rows with PlatformEntry.lock_env, BUILTIN_CREDENTIALS / BUILTIN_PUBLIC_ENV, Adapter.set_drain_probe,
register_fetchers, _jws.py, _identity.py, _http.py, per-platform target rules in gateway/adapters/targets.py, setup wizard
STEPS in gateway_setup, WhatsApp Node bridge in apps/whatsapp-bridge as the sidecar pattern); P17-01..22. P17 builders L2/L3
run in parallel (service/remote, protocol, tests/integration/service, tests/e2e/service) — do not touch those.
Read in full first: docs/plan/phases/P16-more-platforms.md (your rows, Files, Traps, Acceptance, Run until green), AGENTS.md,
HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (every "P16 …" section, esp. "P16 wave 27 landing notes (integrator)"),
docs/plan/16-what-we-change.md §2.7 P16 rows, docs/notes/P16/, and the code your rows build on.

Slices (each owns only its tasks' files; stand in other slices' pieces only under tests/.../_stubs_<slice>.py with FIRST
LINE "# STAND-IN: removed at integration", and state the interface you assumed):
- E15 = P16-25 Photon adapter and sidecar (apps/photon-sidecar, pnpm workspace like apps/whatsapp-bridge; licence-check the
  Spectrum client from package metadata you can see offline — if it is not on licenses_allow.json or not available offline,
  build the sidecar against our own provider interface (like the WhatsApp bridge) and ship the adapter disabled, recording
  it for a D-id). Add its registry row / target rule / setup step as "Wiring for the lead" (do not edit registry.py).
- E16 = P16-33 native draft streaming (Telegram drafts, Slack streams; off unless display.platforms.<p>.native_stream) +
  P16-34 Telegram DM topics and sticker descriptions. Stream consumer core change: if needed, STOP and propose it exactly.
- E17 = P16-35 Discord voice channels (extra  with exact pins if a library is needed: discord.py voice deps,
  check licences; ffmpeg doctor check; fake voice stream + fake clock) + P16-36 adapter matrix gate and doctor gateway checks
  (contract matrix over every registered platform; doctor section). Photon is being built by E15 in parallel: allow-list it
  in the matrix test as pending with a comment the integrator removes.
No hand-written cryptography anywhere in src/ (use `cryptography`/allowed libraries; HMAC via stdlib `hmac`).
Never pkill by a pattern that could hit other worktrees' processes.

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
