# 00 · Layout and conventions

Every phase file names paths from this page. **Rule:** if a phase needs a path or a folder that is
not here, the phase writer adds it here first (with its job and owning phase), then uses it. The
tree follows `04-architecture.md` §3; where this page is more detailed (sub-modules, test folders),
it is the authority for builders.

Status words: **exists** = landed by P00 (guards and scaffold), **planned (Pnn)** = created by that
phase. Anything not marked exists is planned.

## 1. Repository tree

```
indus-agent/
├─ pyproject.toml            exists (P00): one Python config — project, deps, dev group, ruff,
│                            pytest markers; later import-linter, pyright, uv pin window (P00)
├─ uv.lock                   exists (P00): the Python lock, `uv sync --locked` in CI
├─ .python-version           exists (P00): `3.13` (lowest supported; CI also runs 3.14 from P00-09)
├─ package.json              planned (P00-08): private root of the pnpm workspace, scripts only
├─ pnpm-workspace.yaml       planned (P00-08): workspace globs + minimum release age (14 days)
├─ pnpm-lock.yaml            planned (P00-08): the JS lock, `pnpm install --frozen-lockfile` in CI
├─ .nvmrc                    planned (P00-08): Node version (one line)
├─ biome.json                planned (P00-08): the one TS lint + format config
├─ tsconfig.base.json        planned (P00-08): strict TS settings every package extends
├─ vitest.config.ts          planned (P00-08): root vitest config listing workspace projects
├─ LICENSE                   exists (P00): MIT, "Indus Agent contributors"
├─ THIRD_PARTY_NOTICES.md    exists (P00): reference credit (MIT text verbatim) + deps (P25 completes)
├─ README.md                 exists (P00): what the project is, how to build
├─ AGENTS.md, CLAUDE.md      exist (P00): rules for every agent and person (clean room, names, loop)
├─ HANDOFF.md                exists (P00): two-agent handoff state
├─ .agents/                  exists (P00): mailbox — turn.json, agent-N.json, usage logs, sync and
│                            wait scripts
├─ .editorconfig, .gitignore exist (P00)
├─ flake.nix                 Nix flake (P23); module files under nix/
├─ mkdocs.yml                docs site config for docs/user/ (P25)
├─ .hadolint.yaml, .dockerignore  Dockerfile lint config and build context filter (P23)
├─ .github/
│  ├─ workflows/ci.yml       exists (P00): lint, format, tests, clean-room guards, PR-body check;
│  │                         P00 adds the TS job, pyright, import-linter, 3.14 lane, aggregate gate
│  └─ pull_request_template.md  exists (P00): `Reference:` and `Task note:` lines
├─ src/indus/                the Python package (CLI `indus`; `python -m indus`)
│  ├─ __init__.py, __main__.py  exist (P00)
│  ├─ cli.py                 exists (P00): placeholder entry (`--version`); replaced by the
│  │                         `cli/` package in P06-01 (no phase before P06 edits it)
│  ├─ py.typed               planned (P00-10)
│  ├─ platform/              host facts and command lookup; side-effect free, no env input (P01)
│  │  ├─ facts.py            OS, process/native arch, CPU, RAM, GPU class, runtime predicates
│  │  │                      (WSL, container, Termux), interactive session, summary (P01-01)
│  │  ├─ shapes.py           Observation / Candidate / Resolution / LookupContext / Effort (P01-02)
│  │  ├─ known_dirs.py       the only table of well-known install directories (P01-02)
│  │  └─ locate.py           locate / inspect / probe tiers for commands (P01-02)
│  ├─ foundation/            home, profiles, scope, config, secrets, env file, logging, clock (P01)
│  │  ├─ errors.py           `IndusError` tree with stable codes (P01-20)
│  │  ├─ ids.py              session id minting + shape recogniser, message uids (P01-20)
│  │  ├─ fsio.py             atomic writes, owner-only modes, cross-process file locks (P01-07)
│  │  ├─ home.py             home resolution, root vs profile, secure dirs (P01-03)
│  │  ├─ profiles.py         profile ids, live markers, tombstones, sticky profile (P01-04)
│  │  ├─ preparse.py         `-p` selection before any app import (P01-05)
│  │  ├─ scope.py            the `Scope` value and its context variable (P01-06)
│  │  ├─ layout.py           home skeleton, persona seed, scratch temp dir (P01-21)
│  │  ├─ bootstrap.py        first import of every entry point: stdio, sys.path, scratch env (P01-21)
│  │  ├─ yamlio.py           the only YAML reader/writer (YAML 1.1, round trip) (P01-08)
│  │  ├─ config/             schema.py (P01-09), loader.py (P01-10), writer.py (P01-11),
│  │  │                      backups.py + migrations.py (P01-12), keys.py (P01-13)
│  │  ├─ envfile.py          `.env` reader and locked writer with denylist (P01-14)
│  │  ├─ envload.py          `.env` precedence into the scope's env view (P01-15)
│  │  ├─ secrets.py          `SecretStore`: keychain first, 0600 file fallback (P01-16)
│  │  ├─ redact.py           secret scrubbing used by logs, tools and printing (P01-17)
│  │  ├─ log.py              queue logging, rotation, per-home routing, tags (P01-18)
│  │  └─ clock.py            `Clock` protocol, system clock, configured time zone (P01-19)
│  ├─ protocol/              Pydantic contract: methods, events, server requests, error table;
│  │                         codegen to `packages/protocol` (P04)
│  ├─ state/                 SQLite store per profile (P02)
│  │  ├─ connection.py       open, isolation guard, header probe, quarantine-on-open (P02-01)
│  │  ├─ journal.py          journal mode policy and pragmas (P02-02)
│  │  ├─ writer.py           single writer, BEGIN IMMEDIATE, time-budgeted retry (P02-03)
│  │  ├─ readpool.py         bounded read-only pool, fd headroom (P02-04)
│  │  ├─ registry.py         one shared handle per path, generations (P02-05)
│  │  ├─ schema.py           schema 1, declarative reconcile, chunked migration runner (P02-06)
│  │  ├─ guards.py, errors.py  replaced-file / corruption guards; error buckets + copy (P02-07)
│  │  ├─ sessions.py         session rows, ends, child kinds, flags, model_config (P02-08)
│  │  ├─ listing.py          rich listing, previews, last-active, root→tip projection (P02-09)
│  │  ├─ messages.py         append, codec, uids, id order, replace, size guard (P02-10)
│  │  ├─ rewind.py           rewind, clear, branch (P02-11)
│  │  ├─ compression.py      compression lock, watermark, publish continuation, chains (P02-12)
│  │  ├─ titles.py           sanitise, provenance, uniqueness, lineage numbering (P02-13)
│  │  ├─ fts.py              FTS5 setup, stable projection, fail-open stale marker (P02-14)
│  │  ├─ search.py           query sanitiser, routing, LIKE fallback, hits (P02-15)
│  │  ├─ usage.py            per-session and per-model usage (P02-16)
│  │  ├─ portability.py      JSONL export / import with limits (P02-17)
│  │  ├─ deletion.py         delete, tombstones, chunked IN lists (P02-18)
│  │  ├─ maintenance.py      prune, VACUUM gates, in-process holder check (P02-19)
│  │  ├─ session_lock.py     per-session advisory file lock until P17 (P04-21, D-16.69)
│  │  ├─ session_meta.py     per-session namespaced metadata: goals, loops, heartbeats (P02-23)
│  ├─ gateway_routes.py, delivery_ledger.py   gateway routing index and delivery ledger (P15)
│  ├─ runtime_ledger.py, turn_leases.py       admissions FIFO and durable turn lease (P17)
│  ├─ rooms.py, board.py, projects.py         group rooms, task board, projects (P24)
│  │  ├─ browse.py           read-only open for listings, stats, integrity check (P02-20)
│  │  └─ doctor_report.py    M0 minimal doctor report + `python -m` entry (P02-22)
│  ├─ providers/             model providers (P03; more transports and pools in P08)
│  │  ├─ profile.py          `ProviderProfile` (P03-01)
│  │  ├─ registry.py         lookup by name/alias, custom providers from config (P03-02)
│  │  ├─ bundled.py          bundled profile table (P03-03)
│  │  ├─ hosts.py            URL parsing, exact host matching, API-mode from URL (P03-04)
│  │  ├─ route.py            `RuntimeRoute` resolution and key lookup (P03-05)
│  │  ├─ types.py            normalized messages, tool calls, usage, stream events (P03-06)
│  │  ├─ sse.py              Server-Sent Events parser (P03-07)
│  │  ├─ http.py             httpx client factory, timeouts, user agent (P03-08)
│  │  ├─ transports/         __init__.py registry (P03-06), chat_completions.py (P03-09),
│  │  │                      anthropic.py (P03-10); responses, bedrock, gemini (P08)
│  │  ├─ timeouts.py         stale / read timeout policy (P03-13)
│  │  ├─ streaming.py        stream driver: reconnects, partial stub, breaker (P03-14)
│  │  ├─ classify.py         error classifier (P03-15)
│  │  ├─ retry.py            Retry-After parsing and jittered backoff (P03-16)
│  │  ├─ context_length.py   context window resolution (P03-17)
│  │  ├─ model_list.py       live `/models` listing with cache (P03-17)
│  │  └─ auth/               device code, PKCE, credential pools (P07, P08)
│  ├─ safety/                approvals, redaction layer over foundation/redact.py, child env
│  │                         builder, SSRF, path guards (P05); content_scan.py — the one shared
│  │                         threat/content scanner (P04/P05)
│  ├─ tools/                 registry, toolsets, dispatch, budget + spill (P04, P05); builtin/ (P05)
│  │                         with sub-packages code/, browser/, media/, computer_use/ (P10)
│  ├─ backends/              execution contract and local backend (P05); docker/, ssh/ (P11);
│  │                         checkpoints/, checkpoint_api.py, worktrees.py (P11); modal/,
│  │                         vercel/ and the other remote backends (P21)
│  │  ├─ docker/             settings, probe (client, daemon, host facts, doctor rows), args,
│  │  │                      mounts, env (names only), environment, recovery, reaper (P11-01..07)
│  │  ├─ ssh/                client (argv, control sockets), runner, environment (P11-08/09);
│  │  │                      remote_sync, credential_files, remote_processes, process_receipts,
│  │  │                      process_checkpoint sit beside it in backends/ (P11-10..14)
│  │  ├─ checkpoints/        shadow git store, trigger, rollback, prune (P11-15..18)
│  ├─ core/                  agent, turn loop, prompt builder, compression (P04, P08);
│  │                         subagents/ package (P10)
│  ├─ learning/              memory, skills, review fork, curator (P09); skills/hub/ (P13)
│  ├─ plugins/               manifests, discovery, hooks bus (P13)
│  ├─ mcp/                   MCP client (P13)
│  ├─ cron/                  grammar, job store, ticker, delivery (P14); suggestions, blueprints (P24)
│  ├─ board/                 task board dispatcher and daemon watchers; same layer as cron (P24);
│  │                         the board store itself is `state/board.py`
│  ├─ pm/                    `indus pm` helper-binary manager: lock, store, fetch (P24); layer with
│  │                         providers and safety so tools and backends can ask it for paths
│  ├─ service/               SessionService, broker, event fan-out, in-process host (P04, P17);
│  │                         commands/ — the slash-command registry shared by cli, tui_bridge,
│  │                         acp and gateway (P06); ws_auth.py single-use WS tickets (P17);
│  │                         terminal_host.py — session lineage and liveness for the backends (P11)
│  ├─ tui_bridge/            JSON-RPC server for the TUI (P12)
│  ├─ gateway/               daemon, messaging core, adapters/<platform>/ (P15, P16, P17); sub-packages
│  │                         core/, outbound/, delivery/, intake/, daemon/, service/, http/, ws/,
│  │                         adapters/ (P15–P16), multiplex/, rooms/, relay/, browser_broker/ (P24)
│  ├─ web/                   dashboard server (P18)
│  ├─ acp/                   ACP adapter (P19)
│  ├─ mcp_serve/             MCP server mode (P19)
│  ├─ cli/                   click app replacing cli.py (P06-01); update/ sub-package (P23)
│  └─ research/              batch runner, converter, compressor (P22); evals/ (P22)
├─ apps/                     TypeScript surfaces
│  ├─ tui/                   @indus/tui (P12)
│  ├─ web/                   @indus/web (P18)
│  ├─ desktop/               @indus/desktop (P20); resources/ (icons) and scripts/ (build helpers)
│  │                         (P20-38); build/ packaging resources (P23)
│  ├─ whatsapp-bridge/       @indus/whatsapp-bridge — loopback WhatsApp Web sidecar (P16-02)
│  └─ photon-sidecar/        @indus/photon-sidecar — loopback Spectrum sidecar (P16-25)
├─ packages/
│  ├─ protocol/              @indus/protocol — package.json + tsconfig skeleton (P00-08);
│  │                         generated TS types, never hand-edited (P04)
│  ├─ client/                @indus/client — JSON-RPC channel shared by tui/web/desktop (F1 track)
│  ├─ ui/                    @indus/ui — design tokens (P00-12), React components (P18, P20)
│  │  ├─ package.json, tsconfig.json, src/index.ts   planned (P00-08)
│  │  └─ src/tokens/         palette, type, space, radius, motion, themes + tests (P00-12)
│  └─ i18n/                  @indus/i18n — catalogue loader for TS apps (P12)
├─ locales/                  en.yaml (P00-14); TUI key list later (P12); English only (D-16.51)
├─ skills/                   our ~15 starter skills, written fresh (P09)
├─ docker/                   image and Compose (P23); sandbox/ (our sandbox image, P11/P23), s6/
│                            (supervisor service files, P23)
├─ nix/                      flake modules and packages (nix/*, P23)
├─ scripts/
│  ├─ install/sh/, install/ps/   POSIX and PowerShell installers (P23)
│  ├─ build_hooks/           build-time hooks for packaging (P23)
│  ├─ release/               release controller (P23); smoke/ post-publish smoke checks
│  ├─ termux/                Termux APT channel build scripts (P24-21, if capacity)
│  ├─ parity/                REA parity tooling (P25)
│  ├─ docs/                  docs-site helpers (P25)
│  └─ dev/                   developer-only helpers, e.g. record_capture.py (P03-11)
├─ parity/                   parity pass outputs (P25); ledger/ the parity ledger
├─ contributors/             contributor attribution mapping, starts empty (S16, P25; R10.29)
├─ tooling/
│  ├─ checks/                exists (P00): clean-room and repo guards, stdlib only
│  │  ├─ __main__.py, common.py, ledger.py, simtext.py   exist: runner, shared helpers
│  │  ├─ similarity.py, banned.py, assets.py, licenses.py, traps.py, acceptance.py, prbody.py
│  │  │                      exist: the seven guards
│  │  ├─ pins.py             planned (P00-09): dependency pin policy (D-16.52)
│  │  ├─ locales.py          planned (P00-14): catalogue key parity
│  │  ├─ *.json              exist: banned patterns, similarity / licence allow-lists, reference pin
│  │  └─ tests/              exist: one test file per check + synthetic fixtures
│  └─ perf/                  performance budget scripts (P25)
├─ tests/                    Python tests (layout in §3)
│  ├─ conftest.py            planned (P00-11): shared fixtures (tmp home, fake clock)
│  ├─ test_smoke.py          exists (P00)
│  ├─ unit/<package path>/   one folder per source package, e.g. tests/unit/foundation/config/
│  ├─ contract/<area>/       suites every implementation of a contract must pass
│  ├─ integration/<area>/    real files, real SQLite, subprocesses, several modules together
│  ├─ e2e/                   the `indus` command end to end (subprocess or CliRunner)
│  ├─ docker/                image tests (P23, marker `docker`)
│  ├─ security/              security review regression tests (P25)
│  ├─ perf/                  performance budget tests (P25)
│  ├─ unit/{packaging,release,ci,parity,docs}/  tooling-side unit tests (P23, P25)
│  ├─ trials/<decision>/     decision trials, e.g. trials/async_core/ for D-04.1 (P00-13, P03-19)
│  ├─ fakes/                 planned (P00-11): clock.py, model/, platform/, backend/; later phases
│  │                         add sibling fakes (tools.py, store.py in P04)
│  ├─ fixtures/<area>/       test data: captures/ (P03), model_scripts/*.yaml (P03-18 format,
│  │                         P04+ scripts), config samples (P01), stores (P02)
│  └─ golden/<area>/         byte-exact golden outputs, e.g. prompt_surface/ (P04-22)
└─ docs/
   ├─ plan/                  decisions, research, phase files (this folder)
   ├─ notes/                 exists: README.md, _template.md, Pnn/<slug>.md (one per task)
   ├─ internals/             exists: traps-ledger.md; acceptance-map.md (D-15.3), reports
   └─ user/                  user docs (from P06 on; site built by mkdocs.yml in P25)
```

Package rules:

- **Layer order** (import-linter contract, P00-10): `indus.platform` → `indus.foundation` →
  (`indus.protocol`, `indus.state`) → (`indus.providers`, `indus.safety`, `indus.pm`) → `indus.backends` →
  `indus.tools` (D-04.7) → `indus.core` → (`indus.learning`, `indus.plugins`, `indus.mcp`,
  `indus.cron`, `indus.board`) → `indus.service` → `indus.research` (P22: it runs agents through
  the service and the surfaces use it; lead, 2026-10-19) → (`indus.cli`, `indus.tui_bridge`,
  `indus.gateway`, `indus.web`, `indus.acp`, `indus.mcp_serve`). A module imports only from layers to its left
  (and its own layer's siblings only where the contract lists them as independent).
- **No import-time side effects.** No module reads the home, the environment or a config file at
  import time; everything takes a `Scope` (architecture rule 4). `foundation/preparse.py` and
  `foundation/bootstrap.py` are the only modules that touch `sys.argv` / process env, and only
  when called.
- **Stdlib-named modules.** Do not name a module after a stdlib module it would be confused with
  inside the same package (`logging.py`, `platform.py`, `secrets.py` at the top of `indus` are
  banned; `foundation/secrets.py` is fine because imports are absolute). Use `from __future__
  import annotations` and absolute imports everywhere.

## 2. Conventions

| Topic | Rule |
|---|---|
| Python | **3.13 minimum** (`requires-python = ">=3.13"`, ruff `target-version = "py313"`, pyright `pythonVersion = "3.13"`); CI tests 3.13 and 3.14 (architecture D-04 #1 also wants `<3.15`; P00-10 sets it). One interpreter line, no per-dependency interpreter markers (R10.12). |
| Python env | **uv** only: `uv sync`, `uv run …`, `uv lock`. Never `pip install` into the project env. Optional extras per backend/platform (`[project.optional-dependencies]`), added by the phase that needs them. |
| Python style | ruff lint + format, one config in `pyproject.toml` (line 100, rules `E F W I B UP SIM` plus `TID251` banned APIs from P00-11); no second ruff config file (R10.13). Type hints on every public function; `pyright` strict on `foundation`, `protocol`, `state`, `service`, `core`, basic elsewhere (P00-10). |
| Async | asyncio-first (D-04.1, trial in P00-13/P03-19). Blocking work goes through one helper with a deadline (P04); providers are async (`httpx.AsyncClient`). The SQLite writer runs on its own thread executor (P02-03). |
| Errors | Library code raises `IndusError` subclasses with a stable `code` (P01-20); boundaries convert. Never `except Exception: pass`; log once with context. No raw tracebacks to users or models. |
| Paths and files | `pathlib.Path` only. Every write to a user-owned file goes through `foundation/fsio.py` (atomic, owner-only). Every YAML read/write goes through `foundation/yamlio.py`. Home-relative paths come from the `Scope`, never `Path.home()` outside `foundation/home.py`. |
| Tests layout | `tests/unit/<package path>/test_<module>.py` mirrors `src/indus/<package path>/<module>.py`. Contract suites in `tests/contract/`, multi-module and real-I/O tests in `tests/integration/`, CLI runs in `tests/e2e/`, decision trials in `tests/trials/`. Test data in `tests/fixtures/<area>/`; fakes in `tests/fakes/`. Guards' own tests stay in `tooling/checks/tests/`. |
| Test markers | Registered in `pyproject.toml` with `--strict-markers`: `slow` (more than ~2 s; runs in CI, may be skipped locally with `-m "not slow"`), `ref` (needs the pinned reference via `INDUS_REF`), `network` (real network; off in default CI, run in a weekly lane). Added in P23 (P23-01): `platforms(...)` (host OS gating, at most one per test, R10.21) and `docker` (needs a Docker daemon). |
| Time in tests | **No real sleeps — inject clocks.** Code that waits takes a `Clock` (P01-19) and tests pass `tests/fakes/clock.FakeClock`. `time.sleep` and `asyncio.sleep` are banned by ruff `TID251` everywhere except `foundation/clock.py` and `tests/fakes/clock.py` (P00-11). Timing measurements (latency budgets) are allowed only in `slow` tests and only measure, never wait. |
| Fakes | `tests/fakes/model` (scriptable provider speaking real wire formats, P00-11 → P03-18), `tests/fakes/platform` (messaging, P00-11 → P15), `tests/fakes/backend` (terminal backend, P00-11 → P05). Fakes are deterministic: no randomness without a seed, no wall clock. |
| Network in tests | None. HTTP goes through `httpx.MockTransport` driven by fakes or recorded captures (`tests/fixtures/captures/`). Recording new captures is a manual developer step (`scripts/dev/record_capture.py`), never a test. |
| TypeScript | **pnpm** workspaces (`apps/*`, `packages/*`), Node pinned in `.nvmrc` (24 LTS, see §4), TS strict (`strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `verbatimModuleSyntax`), ESM only, `"type": "module"`. Package names `@indus/<name>`; each package has `src/index.ts` as its only entry (`exports` field), no deep imports across packages. |
| TS lint / format / types | **Biome** (one `biome.json` at the root; lint + format) and `tsc --noEmit -p <pkg>` per package. No eslint/prettier. |
| TS tests | **vitest** unit tests beside sources as `*.test.ts` / `*.test.tsx`; Testing Library for React (P18+), ink-testing-library for the TUI (P12); **Playwright** for browser and Electron E2E (`apps/<app>/e2e/*.spec.ts`, from P18). Fake timers (`vi.useFakeTimers()`) instead of real waits. |
| File naming | Python: `snake_case.py`, packages lowercase. TS: `kebab-case.ts`; React components `PascalCase.tsx`; tests mirror the file name. YAML catalogues `locales/<lang>.yaml`. Task notes `docs/notes/Pnn/<slug>.md` (slug = kebab-case of the task's subject). |
| Plan id prefixes | Phase tasks `Pnn-nn` (`P02-07`); features `<area letter><n>` from `03-feature-inventory.md` (`B12`, install/CI area is `S`); acceptance ids `ACC-<feature>-<n>` (`ACC-B12-1`); research traps `R<nn>.<n>`, phase-local traps `T<nn>.<n>`; decisions `D-<doc>.<n>` (`D-16.53`, `D-04.1`); product decisions `P1`–`P8` in `README.md` (never confuse with phase ids `Pnn`). |
| Product id shapes | Session ids `YYYYMMDD_HHMMSS_<hex>` (6 hex interactive, 8 gateway, 12 imports); cron runs `cron_<job>_<YYYYmmdd_HHMMSS>`; background `bg_<HHMMSS>_<6hex>`; API `api_<epoch>_<8hex>`; message uid 32 hex. All minted and recognised only in `foundation/ids.py` (P01-20); a new shape is added there or nowhere. |
| Names | Product **Indus Agent**, CLI/package `indus`, env prefix `INDUS_`, home `~/.indus` (`%LOCALAPPDATA%\indus`), persona file `PERSONA.md`, project file `INDUS.md` / `.indus.md` (D-16.65), UA `indus/<version>`. Vendor-standard credential names stay (`OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`; D-16.22). Glossary: `17-naming-and-glossary.md`. |
| Banned names | The reference's names (`17-naming-and-glossary.md` §d; data in `tooling/checks/banned_patterns.json`) never appear under `src/`, `apps/`, `packages/`, `scripts/`, `tests/`, `tooling/` (except the banned-list data and its tests). Reference names may appear only in `docs/` and the root `README.md`, `AGENTS.md`, `CLAUDE.md`, `THIRD_PARTY_NOTICES.md`. |
| Copy rule | Every string a user or model sees (prompts, help, errors, UI copy, skill text, locale entries, fixture prose) is written fresh (D-16.23). No reference code, comments, docs, test bodies or assets are pasted or paraphrased line by line, in code or in task notes. Third-party protocol names (SQLite pragmas, SSE fields, vendor API fields) are kept exactly as the vendor spells them. |
| Brand | Our own palette, type scale and theme names in `packages/ui/src/tokens/` (P00-12). No reference fonts, glyphs, logos or theme names (R09.22). Fonts are referenced by family name with system fallbacks until an OFL font is vendored with its licence (owner decision, P18). |
| Dependencies | Exact pins (`==` in Python, exact versions in `package.json`, no `^`/`~`); a bounded range only where a library demands it, with a comment saying why. Every version is **at least 14 days old** at lock time: `[tool.uv] exclude-newer` and `minimumReleaseAge: 20160` in `pnpm-workspace.yaml`. Git sources pin a full 40-char SHA. The pin check (`tooling/checks/pins.py`, P00-09) enforces exactly this (D-16.52, R10.11). A new dependency is added only by the task that names it in its phase file. |
| Licences | Runtime dependencies must carry a licence on the allow-list in `tooling/checks/licenses_allow.json`: MIT, BSD-2-Clause, BSD-3-Clause, ISC, Apache-2.0, PSF-2.0, Python-2.0, MPL-2.0 (unmodified only), Unlicense, 0BSD, CC0-1.0. Anything else needs a written exception with a reason in that file. Dev-only tools are reported, not gated. |
| Logging | `logging.getLogger(__name__)` only; handlers are configured once by `foundation/log.py`. Never `print` in library code; in stdio protocol modes nothing but protocol frames reaches stdout (R09.14). |
| Secrets | Never in `config.yaml`, never in logs (redaction filter), never in test fixtures (use obviously fake values such as `test-key-0000`). Read through `SecretStore` / the scope's env view only. |
| Commits | Builders never commit; the lead lands **one commit per slice** with the prefix `[Agent N]` and `git config user.name "Agent N"`, e.g. `[Agent 1] P02-03 writer: time-budgeted retry`. Never force-push or rewrite pushed history. Commit bodies end with the attribution lines the session asks for. |
| Pull requests | Body carries `Reference:` (reference files and Evidence ids read, or `none` and why) and `Task note:` (the note path) — checked by `tooling/checks/prbody.py`. One phase slice per PR. |
| Task notes | Written before code from `docs/notes/_template.md`; the `Read:` line lists every reference file read; the compare checklist is filled after the rewrite; ends with a Status line. |
| Ledgers | Traps a phase pins move from `Review:` to a test reference in `docs/internals/traps-ledger.md` in the same slice; acceptance ids land in `docs/internals/acceptance-map.md` from P06/P07 (D-15.3). |

## 3. Tests: where things go

| Kind | Folder | Rules |
|---|---|---|
| Unit | `tests/unit/<pkg>/` | One module under test; fakes for everything outside it; under 1 s each. |
| Contract | `tests/contract/<area>/` | A parametrised suite that every implementation of an interface must pass (transports, `SecretStore` backends, terminal backends, platform adapters). Adding an implementation means adding it to the parameter list. |
| Integration | `tests/integration/<area>/` | Real files in `tmp_path`, real SQLite, subprocesses; still no network and no real sleeps. Stress and multi-process tests carry `slow`. |
| E2E | `tests/e2e/` | Runs `indus` (subprocess with `INDUS_HOME=<tmp>`) or the CLI app object; asserts stdout, stderr, exit codes. |
| Trials | `tests/trials/<decision>/` | Measurements for a decision (D-04.1, D-04.6 is in integration); each test prints its measured numbers and asserts the pass threshold. |
| Fixtures | `tests/fixtures/<area>/` | Data only: YAML samples, recorded captures, fake-model scripts, small stores. Captures are redacted; never hand-edit a capture to make a test pass. |
| Golden | `tests/golden/<area>/` | Byte-exact expected outputs regenerated only by an explicit `--update-golden` run whose diff is reviewed. |

Every test runs with `INDUS_HOME` pointing into `tmp_path` (autouse fixture in `tests/conftest.py`,
P00-11); the store's test-isolation guard (P02-01) refuses the real home as a second line of
defence.

## 4. Dependency pins

Versions below were checked on 2026-10-18 against the 14-day rule (released on or before
2026-10-04). A phase that adds a dependency adds its row here in the same slice.

| Package | Pin | Released | Licence | Group | Added by |
|---|---|---|---|---|---|
| pytest | 9.1.1 | (exists) | MIT | dev | P00 (exists) |
| ruff | 0.16.10 | (exists) | MIT | dev | P00 (exists) |
| import-linter | 2.15 | 2026-09-04 | BSD-2-Clause | dev | P00-10 |
| pyright | 1.1.414 | 2026-09-10 | MIT | dev | P00-10 |
| pytest-asyncio | 1.4.0 | 2026-05-26 | Apache-2.0 | dev | P00-11 |
| hypothesis | 6.168.3 | 2026-09-28 | MPL-2.0 | dev | P00-11 |
| respx | 0.23.1 | 2026-04-08 | BSD-3-Clause | dev | P03-08 |
| pydantic | 2.13.5 | 2026-08-28 | MIT | runtime | P01-09 |
| ruamel.yaml | 0.19.1 | 2026-01-02 | MIT | runtime | P01-08 |
| keyring | 25.7.0 | 2025-11-16 | MIT | runtime | P01-16 |
| httpx | 0.28.1 | (stable line; 1.0 is still dev) | BSD-3-Clause | runtime | P03-08 |
| anyio | 4.15.1 | 2026-09-05 | MIT | runtime (via httpx) | P03-08 |
| click | 8.5.0 | 2026-08-26 | BSD-3-Clause | runtime | P06-01 |
| prompt_toolkit | 3.0.53 | 2026-07-26 | BSD-3-Clause | runtime | P06-01, P06-04 |
| rich | 15.0.0 | 2026-04-12 | MIT | runtime | P06-01, P06-05 |
| pexpect | 4.9.0 | 2023-11-25 | ISC | dev | P06-01 (for P06-14) |
| croniter | 6.2.4 | 2026-07-10 | MIT | runtime | P14 |
| fastapi | 0.142.2 | 2026-09-30 | MIT | runtime | P15-30 (gateway API server, webhooks) |
| uvicorn | 0.54.0 | 2026-09-25 | BSD-3-Clause | runtime (already locked via mcp) | P15-30 |
| wsproto | 1.3.2 | (already locked via httpx-ws) | MIT | runtime: uvicorn's WebSocket protocol for the daemon attach endpoint | P17-13 |
| mcp (MCP Python SDK) | 2.3.0 | 2026-10-02 | MIT | runtime | P13 (client), P19 (server) |
| jsonschema | 4.26.0 | 2026-01-07 | MIT | runtime | P04/P13 (whichever lands first) |
| playwright | 1.63.0 | 2026-09-15 | Apache-2.0 | extra `browser` | P10 |
| sounddevice | 0.5.6 | 2026-08-17 | MIT | extra `voice` | P10 |
| faster-whisper | 1.2.1 | 2025-10-31 | MIT | extra `voice` | P10 |
| ddgs | 9.16.0 | 2026-08-26 | MIT | extra `web` | P05-23 |
| pypdf | 6.19.0 | 2026-09-16 | BSD-3-Clause | extra `docs` | P05-17 |
| python-docx | 1.2.0 | 2025-06-16 | MIT | extra `docs` | P05-17 |
| openpyxl | 3.1.5 | 2024-06-28 | MIT | extra `docs` | P05-17 |
| ptyprocess | 0.7.0 | 2020-12-28 | ISC | extra `pty` | P05-13 |
| ptyprocess | 0.7.0 | 2020-12-28 | ISC | extra `dashboard` (the Chat tab's PTY on POSIX) | P18-07 |
| pywinpty | 3.0.5 | 2026-06-10 | MIT (PyPI classifier) | extra `dashboard`, Windows only (`sys_platform == 'win32'`): ConPTY for the Chat tab; 3.1.0 (2026-10-15) is inside the 14-day window | P18-07 |
| discord.py | 2.7.1 | 2026-03-03 | MIT | extra `discord` | P15-34 |
| audioop-lts | 0.2.2 | 2025-08-05 | PSF-2.0 | extra `discord` (via discord.py, Python 3.13+) | P15-34 |
| discord.py[voice] | 2.7.1 | 2026-03-03 | MIT | extra `discord-voice` (discord.py's own voice extra on the `discord` pin) | P16-35 |
| PyNaCl | 1.5.0 | 2022-01-07 | Apache-2.0 (bundles libsodium, ISC) | extra `discord-voice` (via discord.py[voice], `<1.6`) | P16-35 |
| davey | 0.1.6 | 2026-06-22 | MIT (wheel metadata classifier) | extra `discord-voice` (via discord.py[voice], `>=0.1.0`; prebuilt wheels for CPython 3.13 / 3.14) | P16-35 |
| slack-bolt | 1.30.0 | 2026-07-15 | MIT | extra `slack` | P15-35 |
| slack-sdk | 3.44.1 | 2026-09-03 | MIT | extra `slack` | P15-35 |
| aiohttp | 3.14.3 | (already locked via modal / daytona) | Apache-2.0 AND MIT | extra `slack` (named: Bolt's async app and Socket Mode handler) | P15-35 |
| httpx-ws | 0.9.0 | (already locked via daytona) | MIT | extras `mattermost`, `dingtalk`, `wecom`, `qqbot`, `feishu`, `simplex`, `buzz` and `yuanbao` (the event sockets, the Feishu / Lark long connection with no lark-oapi, the SimpleX daemon socket, the Nostr relay socket and the Yuanbao bot socket; brings wsproto 1.3.2, MIT, also already locked) | P16-08, P16-14, P16-16, P16-17, P16-20, P16-21, P16-28, P16-29 |
| coincurve | 21.0.0 | 2025-03-08 | MIT OR Apache-2.0 (wheels bundle libsecp256k1, MIT, and cffi's `_cffi_backend`, MIT; licence read from the installed dist-info) | extra `buzz` (BIP-340 Schnorr signing and verification of Nostr events; no runtime dependencies; imported lazily) | P16-29 |
| cryptography | 50.0.2 | (already locked via keyring → SecretStorage on Linux; uploaded 2026-09-30) | Apache-2.0 OR BSD-3-Clause | extras `wecom` (AES-256-CBC for the WeCom callback crypto and media), `google-chat` (RS256 for Google Chat) and `weixin` (AES-128-ECB for iLink media, the vendor's scheme); also base-installed through mcp → pyjwt[crypto], which the Teams bot-token check (P16-11) relies on; imported lazily | P16-11, P16-13, P16-18, P16-19 |
| mautrix | 0.21.1 | 2026-07-05 | MPL-2.0 (file-level copyleft, allow-listed for unmodified dependency use; never patched or vendored) | extra `matrix-e2ee` (Linux only: `sys_platform == 'linux'`): the Olm machine, cross-signing and SQLite crypto store; imported lazily by `matrix/crypto.py` | P16-07 |
| python-olm | 3.2.16 | 2023-11-28 | Apache-2.0 (builds and bundles libolm 3.2.16, Apache-2.0; libolm is deprecated upstream with published timing side-channel advisories, behind our own Engine interface until a vodozemac engine) | extra `matrix-e2ee` (Linux only; no cp313 wheel, built from the sdist with cmake and a C++ compiler) | P16-07 |
| pycryptodome | 3.23.0 | 2025-05-17 | BSD-2-Clause and public domain (LICENSE.rst) | extra `matrix-e2ee` (Linux only), via mautrix | P16-07 |
| unpaddedbase64 | 2.1.0 | 2021-03-09 | Apache-2.0 | extra `matrix-e2ee` (Linux only), via mautrix | P16-07 |
| base58 | 2.1.1 | 2021-10-30 | MIT | extra `matrix-e2ee` (Linux only), via mautrix (recovery-key encoding) | P16-07 |
| aiosqlite | 0.22.1 | 2025-12-23 | MIT | extra `matrix-e2ee` (Linux only): the device store's SQLite driver | P16-07 |
| asyncpg | 0.31.0 | 2025-11-24 | Apache-2.0 | extra `matrix-e2ee` (Linux only): imported by mautrix's crypto-store module (no PostgreSQL is used) | P16-07 |
| agent-client-protocol | 0.12.1 | 2026-08-16 | Apache-2.0 (no licence metadata on PyPI; `LICENSE` read in the 0.12.1 sdist, sha256 `9963ccb5…0095`; needs pydantic only; schema v1.19.0, `PROTOCOL_VERSION` 1) | extra `acp` + dev (the ACP tests run in the gate without extras) | P19-01 |
| Node | 24.21.0 LTS | 2026-09-07 | MIT | `.nvmrc` | P00-08 |
| pnpm | 12.9.1 | 2026-10-03 | MIT | `packageManager` | P00-08 |
| typescript | 6.0.3 | 2026-04-16 | Apache-2.0 | dev | P00-08 (7.x native compiler reconsidered at P12) |
| @biomejs/biome | 2.5.15 | 2026-09-30 | MIT OR Apache-2.0 | dev | P00-08 |
| vitest | 5.0.3 | 2026-09-30 | MIT | dev | P00-08 |
| @types/node | 24.19.1 | 2026-10-01 | MIT | dev | P00-08 |
| @playwright/test | 1.63.0 | 2026-09-04 | Apache-2.0 | dev | P18 (pinned here for reference) |
| react-dom | 19.3.0 | 2026-09-09 | MIT | `@indus/web` runtime | P18-17..22 |
| react-router | 8.4.0 | 2026-09-15 | MIT | `@indus/web` runtime | P18-17..22 |
| @tanstack/react-query | 5.104.1 | 2026-10-02 | MIT | `@indus/web` runtime | P18-17..22 |
| @radix-ui/react-dialog | 1.1.23 | 2026-07-24 | MIT | `@indus/web` runtime | P18-17..22 |
| @radix-ui/react-switch | 1.3.7 | 2026-07-24 | MIT | `@indus/web` runtime | P18-17..22 |
| tailwindcss | 4.3.3 | 2026-07-16 | MIT | `@indus/web` dev | P18-17..22 |
| @tailwindcss/vite | 4.3.3 | 2026-07-16 | MIT | `@indus/web` dev | P18-17..22 |
| @testing-library/react | 16.3.3 | 2026-08-27 | MIT | `@indus/web` dev | P18-17..22 |
| @testing-library/dom | 10.4.2 | 2026-09-13 | MIT | `@indus/web` dev | P18-17..22 |
| @types/react | 19.3.0 | 2026-09-09 | MIT | `@indus/web` dev (already locked for `@indus/tui`) | P18-17..22 |
| @types/react-dom | 19.3.0 | 2026-09-09 | MIT | `@indus/web` dev | P18-17..22 |
| axe-core | 4.13.0 | 2026-08-05 | MPL-2.0 (unmodified) | `@indus/web` dev | P18-17..22 |
| jsdom | 29.1.1 | 2026-04-30 | MIT (pulls dev-only MIT-0 `@csstools/*` and BlueOak-1.0.0 `lru-cache`; 30.x needs Node >= 22.22.2) | `@indus/web` dev | P18-17..22 |

If a pin cannot be installed (yanked, platform wheel missing), the builder takes the newest
version that is at least 14 days old, records the swap in the task note and in this table.

## 5. The shared run-until-green loop

Every task runs this loop before handing back; every phase's "Run until green" section adds its
own commands to it.

```bash
uv sync --locked                                  # Python env exactly as locked
uv run ruff check .                               # lint
uv run ruff format --check .                      # formatting (drop --check to fix)
uv run pyright                                    # types (from P00-10)
uv run lint-imports                               # layer contract (from P00-10)
uv run pytest -q <the paths you touched>          # targeted tests first
uv run pytest -q                                  # whole suite before handing back
uv run python -m tooling.checks --require-ref     # similarity, banned, assets, licences, ledgers, pins, locales
pnpm install --frozen-lockfile                    # TS phases (from P00-08)
pnpm lint && pnpm typecheck && pnpm test          # Biome, tsc, vitest
```

On any failure fix the cause and rerun the whole loop from `uv sync`. Never skip or `xfail` a test
to get green, never widen an allow-list (similarity, licences, banned) without a written reason in
the allow-list entry, and never hand-edit a recorded capture or golden file to make a test pass.
On the shared build machine run pytest without xdist and only your paths until the final run.
