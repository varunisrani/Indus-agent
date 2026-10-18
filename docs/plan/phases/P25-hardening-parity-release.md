# P25 · Hardening, parity, release → v1.0

**Weeks:** 44–45 (2 weeks; P24 grew to 3.5 weeks) · **Tracks:** B (security, ledgers, perf, notices, gates), F (web/desktop security and perf, docs site) · **Depends on:** all phases P00–P24 · **Status:** planned

## Goal
Turn a feature-complete build into a release candidate the owner can tag. When this phase is done:
a security review has walked every trust boundary and its findings are fixed or accepted in
writing; the REA parity pass has run every recorded flow on both apps and every divergence is
classified (intended with a D-id, bug fixed, or reference gap); the obligation ledger has no open or
failed rows; `check:acceptance --strict` and the traps check are green with every research trap
(261 R-ids across the ten specs) mapped to a test or a review row; performance budgets are numbers
in the repo checked on clean runners; the user docs site is built and published;
`THIRD_PARTY_NOTICES.md` is generated from the locks and credits the reference; the similarity
ranking's closest regions each have a verdict; and the release checklist ends with every gate green
from a fresh clone and the line "Ready for `v1.0.0` at `<sha>`". Agents never create the tag,
dispatch the stable release or set signing secrets (skill reference 08 §1); they stop and ask.

Interop note: REA (rea-agents, version recorded in `docs/notes/P25/rea-version.md`; the dry run
used 6.7.0) is a third-party tool: validate every scenario file against that version's own
scenario contract before the first run, and run scenarios through REA itself from day one, never
through a home-made interpreter. REA registration is an open owner decision (D-16.61); without it,
P25-05 to P25-09 run only the product side and every reference-dependent obligation reads
`blocked: REA not registered`, which the release checklist lists as a must-fix for the owner.
OSV and `pnpm audit` data formats, MkDocs (site generator, pinned), and SPDX licence identifiers
are third-party: check against their docs at the pinned versions.

## Tasks (24)
| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P25-01 | **Security review: threat model and static guards.** Write `docs/internals/security-review.md`: assets (keys, sessions, host files), actors (model output, prompt-injected content, other local users, network peers, plugin and MCP authors), and one row per trust boundary (approvals and floors, child env builder, Docker/SSH/SDK backends, SSRF policy, messaging authz and pairing, API server key, dashboard auth gates and WS upgrades, daemon tickets and owner-only socket, desktop IPC sender checks, CSP, fuses, window-open policy, plugin/hub scanners, update and installer downloads), each with the test that proves it or a finding id `SEC-<n>`. Add static guards to the clean-room runner: no `subprocess` call outside the env builder's helper (allow-list file with reasons), no `shell=True` outside the reviewed list, no listener bound without the auth helper, no `os.environ` copy passed to a child, no `yaml.load` without the safe loader. | `website/docs/user-guide/security.md` [1016: section headings, to list boundaries the reference documents] | `docs/internals/security-review.md`, `tooling/checks/security_static.py`, `tooling/checks/security_allow.json`, `tooling/checks/tests/test_security_static.py` | Seeded violations of each static rule fail; current tree passes; every boundary row names a test path that exists (the check resolves them). |
| P25-02 | **Security regression suite (Python).** One parametrized suite over trust boundaries with fakes: yolo cannot bypass hardline floors or deny globs (R02.4); approval timeout reported as timeout, not deny (R02.5); unattended sessions deny instantly (R02.27); webhook sessions get the restricted tool floor; messaging default-deny for unknown users; API server rejects a missing key on loopback (D-16.26); dashboard refuses non-loopback start without a login provider, rejects the loopback token in gated mode, and every WS path rejects an upgrade without auth (D-16.25, R09.2–R09.4); daemon ticket single-use and owner-only socket mode; plugin hub refuses a `dangerous` scan; installer and updater refuse a digest mismatch. | — (our own suite; boundaries from `16-what-we-change.md` §2.7 and the research traps) | `tests/security/test_trust_boundaries.py`, `tests/security/conftest.py` | All cases green; each case id appears in the security review table. |
| P25-03 | **Security regression suite (desktop and web).** Playwright: every desktop IPC channel invoked from a foreign frame (a guest preview page) is rejected (D-16.18); the packaged app's CSP header and meta present on every renderer page; fuses read from the packaged binary (RunAsNode off, NODE_OPTIONS and inspect args off, ASAR integrity on, only-load-from-ASAR on, cookie encryption on) (D-16.19); guest content cannot open a URL by click, `window.open`, or navigation (D-16.20); web dashboard: login required on a non-loopback bind, host-header guard, WS upgrade without a ticket closed. | `apps/desktop/electron/window-open-policy.ts` [106], `apps/desktop/electron/hardening.ts` [657: the list of guarded operations only] | `apps/desktop/e2e/security-audit.spec.ts`, `apps/web/e2e/auth-gates.spec.ts` | Specs green on the packaged desktop build (Linux headless in CI, macOS/Windows in the release lane) and on the web build. |
| P25-04 | **Dependency audit.** `tooling/checks/deps_audit.py`: OSV query for every package in `uv.lock` and `pnpm-lock.yaml` (offline cache allowed; network lane in CI), plus `pnpm audit --prod`; a finding fails unless it is listed in `tooling/checks/deps_audit_allow.json` with an expiry date and a reason; expired entries fail. Also verifies every pinned binary in `scripts/install/pins.json` and the Docker base digests still resolve. | `.github/workflows/supply-chain-audit.yml` [285: scan and bounds steps, for scope] | `tooling/checks/deps_audit.py`, `tooling/checks/deps_audit_allow.json`, `tooling/checks/tests/test_deps_audit.py` | Fixture lock with a known-vulnerable version fails; allow-listed with future expiry passes; expired fails. |
| P25-05 | **Parity runner (product side).** `scripts/parity/runner.py`: one product server or process per flow, set up from a table in the runner (`--list` prints it: fake model script, demo git project, fresh home, which surface); runs browser flows (dashboard, desktop renderer) through REA's browser scenario capture and terminal flows (classic CLI, TUI, gateway via the fake platform) through our own transcript driver (tmux/PTY capture to a structured event list: regions, cards, prompts, exit codes). One flow at a time; fixed origin `http://127.0.0.1:24000` for browser flows. Product captures go to a gitignored cache. | skill reference 03 §1–§3 (process only; no reference product files) | `scripts/parity/runner.py`, `scripts/parity/terminal_driver.py`, `tests/unit/parity/test_runner.py` | `--list` prints every flow; a dry run against the fake model completes the CLI flow and writes a capture to the cache; a missing step id fails that flow, not the run. |
| P25-06 | **Scripted model server for both apps.** The reference needs model turns; use the public seam both apps have, an OpenAI-compatible base URL. `scripts/parity/scripted_model_server.py` speaks Chat Completions streaming (written from the public API description, no new dependency beyond the HTTP server we already ship) and plays named scripts that **mirror our fake model's scripts by name** (`echo`, `approve_or_decline` (asks to run a command; answers differently when the tool result says denied), `clarify`, `edit_file`, `long_stream`, `hang`), so both apps hold the same conversation. Logs frames only with `--log`. | — (public Chat Completions protocol) | `scripts/parity/scripted_model_server.py`, `tests/unit/parity/test_scripted_model_server.py` | Its own tests (≥ 7): stream framing, tool call emission, deny branch, clarify, edit confined to a temp dir, cancel on client disconnect, unknown script → 400. |
| P25-07 | **Product scenarios.** One file per flow, `parity/<flow>.indus.json`, with stable step ids shared with the reference-side files kept in `~/rea-lab/parity/` (never committed): `first-run-setup`, `cli-chat-approve`, `cli-resume-search`, `tui-stream-interrupt`, `tui-model-picker`, `dashboard-sessions`, `dashboard-chat-tab`, `desktop-onboarding`, `desktop-chat-panes`, `gateway-telegram-approve` (fake platform), `cron-schedule-run`, `acp-session`. Every step has `timeout_ms`; locators match exactly one element using our own `data-ui` hooks; identical environment blocks (viewport, locale `en-US`, timezone UTC, reduced motion); secrets declared by id. | skill reference 03 §2 | `parity/*.indus.json` (12 files), `tests/unit/parity/test_scenarios.py` | Scenario self-test: unique step ids, every step has a timeout, every locator uses a `data-ui` hook, files validate against REA's contract at the pinned version. |
| P25-08 | **Obligation ledger.** `parity/ledger/obligations.json` (`{id: "OBL-<AREA>-<nnn>", title (observable claim, our words), family, scenario, steps[], required}`), `parity/ledger/manifest.json` (exactly one `{obligation, owner: {module, symbol}, tests[]}` per obligation), intended rules (`{scenario, kind, subject, cite (D-id or 16 §), why}`) and reference gaps (`{scenario, steps, why}`). Statuses: verified, open, blocked, failed, not run. Self-test: every scenario proves ≥ 1 obligation; every step named exists; every owner module exports its symbol; every test id resolves; every rule cites a D-id that exists in `16-what-we-change.md`; every gap has a reason. | skill reference 03 §8 | `parity/ledger/obligations.json`, `parity/ledger/manifest.json`, `parity/ledger/rules.json`, `scripts/parity/ledger.py`, `tests/unit/parity/test_ledger.py` | Self-test green; a rule citing a missing D-id fails; a manifest pointing at a non-existent symbol fails; `--ledger` report prints counts per status. |
| P25-09 | **Compare, classify, report.** Run all flows on both apps (reference side by the owner-approved REA sandbox, fresh home per flow, clean env); compare with REA's step-aligned comparison plus our summary kinds (`step-only-in-<side>`, `step-failed-in-<side>`, `role-only-in-<side>` at `wait_for` steps only, `page-errors`); accessible names are not compared. Classify every divergence: intended (cite), bug (fix with a failing-first test in the owning module, rerun the flow), reference gap, or unknown (lead decides, becomes a new D-id). Write `docs/internals/parity-report-v1.md` with commands, versions, counts, limits; correct `16-what-we-change.md` where a capture contradicts it. The runner exits 1 while anything is unclassified. | skill reference 03 §6–§7, §10 | `docs/internals/parity-report-v1.md`, `scripts/parity/compare.py`, `tests/unit/parity/test_compare.py` | Compare unit tests on synthetic captures (alignment failure on duplicate ids, role-only detection only at `wait_for`, names ignored); final run: 0 unclassified, ledger 0 open, 0 failed. |
| P25-10 | **Acceptance map strict.** Every P0/P1 inventory row has ≥ 1 `ACC-<feature>-<n>` with a test reference `file > describe > test`; P2/P3 rows built in 1.0 get ids too; rows not built are under **Known gaps** citing their §2.8 D-id; changed rows cite their D-id. `--strict` already fails on `Review:` rows (P00); extend `tooling/checks/acceptance.py` so strict mode also fails on any referenced test missing or skipped (collected via `pytest --collect-only -q` and vitest/playwright list output), and any feature id that no longer exists in the inventory (S-ids after the area rename). | skill reference 04 §10 | `tooling/checks/tests/test_acceptance_strict.py` | Seeded pending row fails `--strict`; skipped test fails; renamed feature id fails; real map passes `--strict`. |
| P25-11 | **Traps ledger complete.** `tooling/checks/traps.py` parses every `**R<nn>.<n>` item in `docs/plan/research/*.md` §6 (261 today) and fails when one has no ledger row, when a row's test reference does not resolve, or when a row is still `Review:` without an owner task and a written reason (accepted reviews are allowed only for traps that cannot be tested, each signed by the lead). Fill the remaining rows from every phase's "Traps covered" table. | skill reference 04 §10 | `tooling/checks/tests/test_traps_complete.py` | Removing one row fails with the R-id; an unresolved test path fails; real ledger passes with 261 rows. |
| P25-12 | **Performance budgets (Python).** `tooling/perf/budgets.toml` holds every number; `tests/perf/` measures on clean CI runners (median of 5, injected workloads, no network): `indus --version` < 150 ms (D-04.4); cold start of `indus -z` to first fake token < 1.5 s; 50 concurrent fake sessions in one daemon keep p99 event latency < 50 ms (D-04.1 c); interrupt closes the stream < 200 ms (D-04.1 a); store append of 10,000 messages < 3 s with FTS; session search over 100,000 messages < 300 ms; compression of a 200-turn session < 2 s excluding the model. A regression > 10 % over the recorded baseline fails the perf lane (baseline file updated only with a note). | — (our budgets; numbers from `04-architecture.md` §8 and the trials) | `tooling/perf/budgets.toml`, `tooling/perf/baseline.json`, `tests/perf/test_budgets.py`, `tests/perf/conftest.py` | Perf lane green on clean runners; a synthetic slowdown (an injected slow path through a test hook; no real sleeps) fails with the budget name; perf tests carry the `slow` marker. |
| P25-13 | **Performance budgets (TypeScript).** Web dashboard initial JS ≤ 350 KB gzip and per-route chunks ≤ 200 KB (numbers in `budgets.toml`, checked on a production build); TUI streams a 10,000-token reply at ≥ 30 fps on the recorded stream (D-04.2) and a resize storm of 100 events renders ≤ 10 frames; desktop cold start to interactive chat ≤ 3 s on the CI runner (packaged build, fake backend). | — | `apps/web/scripts/check-bundle.mjs`, `apps/tui/bench/stream-fps.bench.ts`, `apps/desktop/e2e/startup-budget.spec.ts` | Each check reads `tooling/perf/budgets.toml`; an oversized fixture chunk fails the bundle check. |
| P25-14 | **Docs site build.** MkDocs (Material theme, pinned; MIT) over `docs/user/` with `mkdocs.yml`: navigation (Getting started: install per channel, quickstart, Windows/WSL2, updating; User guide: CLI, TUI, desktop, dashboard, profiles, sessions, tools and backends, approvals and security, memory and skills, plugins and MCP, cron, messaging per platform, research; Reference: generated pages; Developer: architecture, protocol, adding a tool/backend/platform), built-in search, strict mode (broken links **fail**), `llms.txt` and `llms-full.txt` generated at build, deploy to GitHub Pages from `.github/workflows/docs.yml` on release publish and on `main` pushes touching docs. All text written fresh (D-16.23); no reference doc text (n-gram check runs over `docs/user/`). | `website/docusaurus.config.ts` [217: structure only], `website/sidebars.ts` [920: section list only], `website/scripts/prebuild.mjs` [174: what is generated] | `mkdocs.yml`, `scripts/docs/llms_txt.py`, `.github/workflows/docs.yml`, `tests/unit/docs/test_docs_build.py` | `mkdocs build --strict` green; llms files list every page; a broken link fails; n-gram check clean over the built site. |
| P25-15 | **Generated reference pages.** `scripts/docs/gen_reference.py` writes, from the code: CLI command reference (click tree with options and help), slash commands (registry), config keys with defaults and types (Pydantic schema, D-16.32), environment variables (`INDUS_*` and vendor-standard names), tools and toolsets (registry), protocol methods/events/error codes (D-16.27 table), MCP serve tools, starter skills list. CI fails when the generated pages are stale. | `website/docs/reference/` [14 pages: titles only, to check coverage] | `scripts/docs/gen_reference.py`, `docs/user/reference/` (generated pages), `tests/unit/docs/test_gen_reference.py` | Generator idempotent; stale check fails after adding a CLI option without regenerating; every registered tool appears. |
| P25-16 | **User guide content.** Write the hand-written pages listed in P25-14's navigation (getting started, each surface, security model, backends incl. P21's four, messaging setup per platform, research tooling incl. `trajectory-format.md` from P22), in our voice, each with one runnable example verified by a docs test where possible (commands run against the fake model). The Docker page states that the Windows compose file needs a configured login provider (no insecure flag, D-16.25) and shows how to set one up. | — (write from our own plan and code; no reference prose) | `docs/user/**/*.md` (hand-written pages), `tests/unit/docs/test_examples.py` | Example commands in fenced `console` blocks marked `test` run green; n-gram and banned-identifier checks clean. |
| P25-17 | **THIRD_PARTY_NOTICES generation.** `scripts/release/notices.py` builds `THIRD_PARTY_NOTICES.md` from `uv.lock` (with installed package metadata), `pnpm-lock.yaml`, the pinned binaries in `scripts/install/pins.json` (uv, Python standalone build, portable Git for Windows with its source-offer note), the Docker base image and s6-overlay, bundled fonts and icons; groups by licence (SPDX ids); includes each licence text once; credits the reference's MIT copyright line verbatim (D-16.40); fails on an unknown or not-allow-listed licence (`tooling/checks/licenses_allow.json`) and on copyleft components that need an obligation note without one. Desktop bundles and the Docker image embed the same file; CI fails when it is stale. | `docs/plan/research/10-research-packaging-ci.md` §8.1 (our dependency/licence table; not a reference file) | `scripts/release/notices.py`, `tests/unit/release/test_notices.py` | Fixture lock with an unknown licence fails; stale file fails; real file lists every runtime dependency; reference credit line present exactly once. |
| P25-18 | **Similarity `--closest` report.** `--closest N` already ranks runs (P00); extend `tooling/checks/similarity.py` so that with N = 20 it ranks the most similar regions between our tree (code, docs, prompts, UI copy) and the reference by the n-gram score, print path:line ranges for both sides and the score, and write `docs/internals/similarity-report.md` with one row per region and a verdict column (`independent` / `protocol-mandated` / `convention` / `fix`); a region with no verdict, or a `fix` verdict, fails the release gate. Not pass/fail by score: a person reads the list. | skill reference 06 §2 | `docs/internals/similarity-report.md`, `tooling/checks/tests/test_similarity_closest.py` | Synthetic repo pair: ranking order; report row per region; missing verdict fails the gate mode. |
| P25-19 | **Clean-room sweep of built artefacts.** Run the banned-identifier and asset-hash checks over **built** outputs, not just sources: unpacked desktop bundles (ASAR contents), the Docker image file list and labels, the built docs site, the generated installers and the web dist; fail on any reference name, reference asset hash or reference host. | — | `tooling/checks/artifacts.py`, `tooling/checks/tests/test_artifacts.py` | Seeded banned name inside a fake ASAR fails; clean build passes. |
| P25-20 | **Release checklist and bug-bash triage.** `docs/internals/release-checklist.md` from the skill template: §0 one-time decisions with records and safe placeholders (owner-open: desktop app ids `ai.indus.*`; Windows installer format is settled as signed NSIS, D-16.70; owner-open: GHCR owner/namespace, docs domain; maintainer contact `<maintainers@indus.invalid>` placeholder, signing secrets by name, trademark search D-16.40, REA registration D-16.61, open D-16 rows); triage of every open item from task notes, `HANDOFF.md`, acceptance Known gaps, security findings and parity ledger into must (`RM<n>`), should (`RS<n>`) and later (`RL<n>`) (the `R` prefix keeps them apart from inventory S-ids); ids permanent; must→should only with the owner's dated written approval; "Done in this wave" table with commits; "After v1.0" list matching `18-phase-wise-build-plan.md`. | skill reference 08 §2–§4 | `docs/internals/release-checklist.md` | Checklist lint (part of the ledger check): ids unique and never reused, counts in headings match rows, every row has Source and Why. |
| P25-21 | **Release gates from a fresh clone.** `scripts/release/release_gates.sh <repo> <sha> <gates file>` clones into a new dir, runs every gate even after a failure, writes `<gate> exit=<n> <s>s` lines and per-gate logs. `scripts/release/gates.txt`: install (`uv sync --locked`, `pnpm install --frozen-lockfile`), lint, format, types (pyright + `tsc`), unit, contract, integration, web e2e, TUI e2e, desktop e2e (packaged), clean room `--require-ref`, acceptance `--strict`, traps, ACP protocol drift vs the pinned SDK schema, MCP drift, bundle budget, perf, docs build strict, notices fresh, dependency audit, parity `--all --ledger`, similarity `--closest 20`, installer build fresh, Docker image tests, `nix flake check`. | skill reference 06 §3, reference 08 §5 | `scripts/release/release_gates.sh`, `scripts/release/gates.txt`, `tests/unit/release/test_release_gates.py` | Runner test with a fake gates file: continues after a failure, exit codes recorded, summary lists every gate; real run on the candidate recorded in `HANDOFF.md`. |
| P25-22 | **Canary dress rehearsal and update from the last canary.** The owner (or an agent with explicit approval) runs the P23 release workflow in canary mode on the candidate commit: dry run first, then real: all desktop targets built and signed, images pushed by digest, installers attached. On each OS a machine with the previous canary installed and its data in place updates with `indus update` (source installs) or the desktop updater: the update is offered without switching channel (canary reads stable too), version reported, data intact (sessions, keychain secrets, profiles, launcher). `tests/e2e/upgrade/test_canary_to_stable.py` automates the source-install half in CI with two locally built releases. | skill reference 08 §6, §8 | `tests/e2e/upgrade/test_canary_to_stable.py`, `docs/internals/rehearsal-log.md` | E2E green in the release lane; rehearsal log lists OS, versions, steps and outcomes. |
| P25-23 | **Post-release smoke and rollback kit.** Per-OS smoke scripts using only public downloads: install, `indus --version`, `setup --non-interactive` with a fake-model endpoint, one turn with an approval, resume, `doctor`, desktop launch to chat, dashboard login on loopback, licences screen present, uninstall keep-data. Rollback instructions prepared for the owner (turn the GitHub Release back to draft, move GHCR aliases back to the previous digest, revert the channel record, fix forward with `1.0.1`; never delete or reuse a version). | skill reference 08 §9 | `scripts/release/smoke/linux.sh`, `scripts/release/smoke/macos.sh`, `scripts/release/smoke/windows.ps1`, `docs/internals/rollback.md` | Scripts pass shellcheck/PSScriptAnalyzer; run green against the canary rehearsal artefacts on three OSes (log in the rehearsal file). |
| P25-24 | **Native Windows terminal backend (best effort, D-16.55).** Local backend on native Windows through Git Bash: locate `bash.exe` via the platform command lookup (Git for Windows install dirs, the installer's portable Git); MSYS path conversion both ways for cwd and file arguments; temp dir under `<home>\cache\terminal` with forward slashes; process tree kill via job objects; UTF-8 console. Gated behind `platforms("windows")` tests; documented as best effort in the Windows guide. | `tools/environments/local.py` [1178: Windows branches only], `website/docs/user-guide/windows-native.md` [383] | `src/indus/backends/local_windows.py`, `tests/unit/backends/test_local_windows.py` | Windows CI lane: contract suite subset (echo, cwd marker, timeout rc 124, interrupt rc 130, kill tree) green; path conversion table test runs everywhere. |

## Files to create (73, plus the docs pages under `docs/user/`)
| Path | What it holds | Task |
|---|---|---|
| `docs/internals/security-review.md` | threat model, boundary table, findings | P25-01 |
| `tooling/checks/security_static.py`, `tooling/checks/security_allow.json`, `tooling/checks/tests/test_security_static.py` | static guards, reasons, tests | P25-01 |
| `tests/security/test_trust_boundaries.py`, `tests/security/conftest.py` | Python security suite | P25-02 |
| `apps/desktop/e2e/security-audit.spec.ts`, `apps/web/e2e/auth-gates.spec.ts` | desktop/web security suite | P25-03 |
| `tooling/checks/deps_audit.py`, `tooling/checks/deps_audit_allow.json`, `tooling/checks/tests/test_deps_audit.py` | dependency audit | P25-04 |
| `scripts/parity/runner.py`, `scripts/parity/terminal_driver.py`, `tests/unit/parity/test_runner.py` | parity runner | P25-05 |
| `scripts/parity/scripted_model_server.py`, `tests/unit/parity/test_scripted_model_server.py` | scripted model server | P25-06 |
| `parity/first-run-setup.indus.json` … `parity/acp-session.indus.json` (12 files), `tests/unit/parity/test_scenarios.py` | product scenarios | P25-07 |
| `parity/ledger/obligations.json`, `parity/ledger/manifest.json`, `parity/ledger/rules.json`, `scripts/parity/ledger.py`, `tests/unit/parity/test_ledger.py` | obligation ledger | P25-08 |
| `docs/internals/parity-report-v1.md`, `scripts/parity/compare.py`, `tests/unit/parity/test_compare.py` | comparison and report | P25-09 |
| `tooling/checks/tests/test_acceptance_strict.py` | strict acceptance tests | P25-10 |
| `tooling/checks/tests/test_traps_complete.py` | traps completeness tests | P25-11 |
| `tooling/perf/budgets.toml`, `tooling/perf/baseline.json`, `tests/perf/test_budgets.py`, `tests/perf/conftest.py` | Python perf budgets | P25-12 |
| `apps/web/scripts/check-bundle.mjs`, `apps/tui/bench/stream-fps.bench.ts`, `apps/desktop/e2e/startup-budget.spec.ts` | TS perf budgets | P25-13 |
| `mkdocs.yml`, `scripts/docs/llms_txt.py`, `.github/workflows/docs.yml`, `tests/unit/docs/test_docs_build.py` | docs site build | P25-14 |
| `scripts/docs/gen_reference.py`, `docs/user/reference/*.md` (generated), `tests/unit/docs/test_gen_reference.py` | generated reference | P25-15 |
| `docs/user/**/*.md` (hand-written), `tests/unit/docs/test_examples.py` | user guide | P25-16 |
| `scripts/release/notices.py`, `tests/unit/release/test_notices.py` | notices generator | P25-17 |
| `docs/internals/similarity-report.md`, `tooling/checks/tests/test_similarity_closest.py` | closest-regions report | P25-18 |
| `tooling/checks/artifacts.py`, `tooling/checks/tests/test_artifacts.py` | built-artefact sweep | P25-19 |
| `docs/internals/release-checklist.md` | checklist and triage | P25-20 |
| `scripts/release/release_gates.sh`, `scripts/release/gates.txt`, `tests/unit/release/test_release_gates.py` | fresh-clone gates | P25-21 |
| `tests/e2e/upgrade/test_canary_to_stable.py`, `docs/internals/rehearsal-log.md` | rehearsal | P25-22 |
| `scripts/release/smoke/linux.sh`, `scripts/release/smoke/macos.sh`, `scripts/release/smoke/windows.ps1`, `docs/internals/rollback.md` | smoke and rollback | P25-23 |
| `src/indus/backends/local_windows.py`, `tests/unit/backends/test_local_windows.py` | Git Bash backend | P25-24 |

## Files to update (11)
| Path | Change | Task |
|---|---|---|
| `tooling/checks/__main__.py` | register `security`, `deps`, `artifacts` checks; `--report` flag for the similarity report | P25-01, P25-04, P25-18, P25-19 |
| `tooling/checks/acceptance.py` | strict mode also fails on missing/skipped tests and unknown feature ids | P25-10 |
| `tooling/checks/traps.py` | parse all research §6 R-ids, resolve test refs, review-row rules | P25-11 |
| `tooling/checks/similarity.py` | region ranking for `--closest`, report writer, verdict gate | P25-18 |
| `tooling/checks/licenses.py`, `tooling/checks/licenses_allow.json` | share the allow-list with the notices generator; obligation notes for copyleft | P25-17 |
| `docs/internals/acceptance-map.md` | every row resolved; Known gaps cite §2.8 | P25-10 |
| `docs/internals/traps-ledger.md` | all 261 rows | P25-11 |
| `THIRD_PARTY_NOTICES.md` | replaced by the generated file | P25-17 |
| `docs/plan/16-what-we-change.md` | new D-ids from the parity pass and security review; corrections from captures | P25-09, P25-01 |
| `.github/workflows/ci.yml` | perf lane, docs build, notices freshness, security and artefact checks | P25-12, P25-14, P25-17, P25-19 |
| `HANDOFF.md` | gate results with exit codes and durations; "Ready for v1.0.0 at <sha>" | P25-21 |

## Fixtures and fakes
| Path | What it holds | Task |
|---|---|---|
| `tooling/checks/tests/fixtures/security/` | seeded violations per static rule | P25-01 |
| `tooling/checks/tests/fixtures/deps/` | lock files with known-vulnerable and unknown-licence packages | P25-04, P25-17 |
| `tests/fixtures/parity/captures/*.json` | synthetic captures (ours, generated) for compare tests; never reference captures | P25-09 |
| `tooling/checks/tests/fixtures/simrepo/` | tiny paired trees for ranking tests | P25-18 |

## Task notes (24)
`docs/notes/P25/<slug>.md`: `security-review.md` (01) · `security-python.md` (02) ·
`security-ts.md` (03) · `deps-audit.md` (04) · `parity-runner.md` (05) · `scripted-model.md` (06) ·
`parity-scenarios.md` (07) · `obligation-ledger.md` (08) · `parity-compare.md` (09) ·
`acceptance-strict.md` (10) · `traps-complete.md` (11) · `perf-python.md` (12) · `perf-ts.md` (13) ·
`docs-site.md` (14) · `docs-reference.md` (15) · `docs-guide.md` (16) · `notices.md` (17) ·
`similarity-closest.md` (18) · `artefact-sweep.md` (19) · `release-checklist.md` (20) ·
`release-gates.md` (21) · `rehearsal.md` (22) · `smoke-rollback.md` (23) · `windows-local.md` (24);
plus `rea-version.md` (interop record).

## Run until green
```bash
uv sync --locked --all-extras && pnpm install --frozen-lockfile
uv run ruff check && uv run ruff format --check && uv run pyright
pnpm -r lint && pnpm -r typecheck && pnpm -r test
uv run pytest tests/unit tests/contract tests/integration tests/security tooling/checks/tests
pnpm --filter @indus/web test:e2e && pnpm --filter @indus/desktop exec playwright test --workers=1
uv run python -m tooling.checks --require-ref
uv run python -m tooling.checks acceptance traps --strict --require-ref
uv run pytest tests/perf && node apps/web/scripts/check-bundle.mjs
uv run mkdocs build --strict && uv run python scripts/docs/gen_reference.py --check
uv run python scripts/release/notices.py --check
uv run python scripts/parity/runner.py --all --ledger           # needs REA (D-16.61)
uv run python -m tooling.checks similarity --require-ref --closest 20 --report
scripts/release/release_gates.sh <repo url> <candidate sha> scripts/release/gates.txt   # final
```
On any failure fix the cause and rerun from the install line. A test that fails under load is a
claim, not a flake: rerun it alone 6× on the head and on the base commit before any verdict (skill
reference 06 §4), and record the counts in the note. Never skip a test, never widen an allow-list
without a dated reason, never commit a reference capture.

## Compare checklist (reopen the reference after writing)
- [ ] Every user-visible flow in the reference's docs navigation has a parity scenario, an acceptance row, or a §2.8 gap.
- [ ] Every intended difference found in the parity pass has a D-id in §2.7 (or §2.8), and the parity rules cite it.
- [ ] The docs site covers every reference docs section at the same depth or records why not; no reference prose.
- [ ] Every trust boundary the reference documents in its security guide has a row in our review.
- [ ] Notices cover every redistributed binary the reference's installer also redistributes (Python, uv, Git for Windows) with their obligations.

## Traps covered
| Trap | Pinned by |
|---|---|
| All 261 research R-ids (R01.1–R10.30) have a test or a signed review row | P25-11 |
| R02.4, R02.5, R02.27 floors and silent-consent rules | P25-02 |
| R09.2–R09.4 dashboard auth gates and WS helper | P25-02, P25-03 |
| R08.4, R08.8 desktop sender checks and window-open policy | P25-03 |
| R09.22 brand assets (no vendor design package or fonts) | P25-19 |
| R10.28 translation parity (English catalogue + mechanism) | P25-11 (ledger row to the P00 check) |
| R10.29 personal data never in the repo | P25-19 |
| R10.30 supply-chain scans are heuristics: paired with OSV | P25-04 |
| R08.22 signing order verified on packaged builds | P25-03, P25-22 |

## Acceptance
| Feature | Planned ids |
|---|---|
| S15 docs website with search and `llms.txt` | ACC-S15-1 (strict build), ACC-S15-2 (llms files list every page) |
| S17 clean-room guards | ACC-S17-3 (built artefacts swept), ACC-S17-4 (closest-regions report has verdicts) |
| S18 THIRD_PARTY_NOTICES | ACC-S18-1 (generated and fresh), ACC-S18-2 (unknown licence fails) |
| S8 dependency policy | ACC-S8-3 (audit with expiring allow-list) |
| Q6 native Windows (terminal backend part) | ACC-Q6-3 (Git Bash backend contract subset) |
| All P0/P1 rows | gated by `check:acceptance --strict` (P25-10) |

## Done when
- From a fresh clone of the candidate commit, `release_gates.sh` reports every gate `exit=0`, CI is
  green on that commit in every workflow, and the run is logged in `HANDOFF.md`.
- The parity ledger has no `open` or `failed` obligation and no unclassified divergence (or, if REA
  is still not registered, every reference-dependent obligation is `blocked` and listed as an owner
  must-fix); the security review has no open finding without the owner's written acceptance.
- The canary rehearsal ran on three OSes and the previous canary updated cleanly.
- The release checklist ends with "Ready for `v1.0.0` at `<sha>`" and the open owner items; an agent
  then stops. **v1.0 demo (M9):** the owner installs the release candidate from the public channels
  (installer, Docker, desktop) on macOS, Windows and Linux and runs one real turn on each.
