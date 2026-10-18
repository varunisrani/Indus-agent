# 04 · Architecture

The reference's architecture is an idea we keep: a Python agent runtime that owns all behaviour,
thin TypeScript front ends, one SQLite store per profile, and (eventually) one gateway process per
profile that every surface attaches to. Every concrete choice below is ours, with our own reason.
Stack decisions are `D-04.n`; product differences live in `16-what-we-change.md`.

## 1. Big picture

```
 SURFACES (thin: present, collect input, answer server requests)
 ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐ ┌──────────┐
 │ CLI REPL │ │   TUI    │ │   Web    │ │ Desktop  │ │   ACP    │ │ Gateway        │ │ MCP serve│
 │ (Python, │ │ (Node,   │ │ dashboard│ │(Electron)│ │ (editors │ │ platforms      │ │ (other   │
 │  rich +  │ │  React + │ │ (React   │ │ main +   │ │  over    │ │ Telegram,      │ │  agents  │
 │  prompt- │ │  Ink)    │ │  SPA +   │ │ renderer │ │  stdio)  │ │ Discord, Slack,│ │  over    │
 │  toolkit)│ │          │ │  xterm)  │ │          │ │          │ │ … API, webhook │ │  stdio)  │
 └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └───────┬────────┘ └────┬─────┘
      │ in-proc    │ JSON-RPC   │ REST + WS  │ REST + WS  │ ACP→svc    │ adapters        │ read store,
      │ (P06)      │ stdio(P12) │ (P18)      │ (P20)      │ (P19)      │ (P15)           │ send via lib
      │            │ WS (P17)   │            │            │            │                 │
 ═════╪════════════╪════════════╪════════════╪════════════╪════════════╪═════════════════╪══════════
      ▼            ▼            ▼            ▼            ▼            ▼                 ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │ RUNTIME (Python package `indus`)                                                              │
 │                                                                                              │
 │  SessionService ── the one API every surface uses (create/resume/submit/interrupt/steer,     │
 │  │                 server requests: approval, clarify, sudo, secret; event stream)            │
 │  │                                                                                           │
 │  ├─ Agent turn loop (core): prompt builder · request assembly · retry/fallback ·            │
 │  │     tool round · compression · usage/cost · interrupts · subagents                         │
 │  ├─ Providers: profiles · transports (chat-completions, anthropic, responses, bedrock,       │
 │  │     gemini) · auth (keys, device code, PKCE) · credential pools · model catalog            │
 │  ├─ Tools: registry · toolsets · dispatch · result budget/spill · safety (approvals,         │
 │  │     redaction, env scrub, SSRF) · file/web/browser/media/code/delegate tools               │
 │  ├─ Learning: memory stores · skills · background review · curator · hub                      │
 │  ├─ Extensions: plugins + hooks · MCP client                                                  │
 │  ├─ Cron: grammar · job store · ticker · delivery targets                                     │
 │  ├─ State store: sessions · messages · FTS · titles · usage · ledgers (SQLite per profile)    │
 │  └─ Foundation: home/profiles · config · secrets (keychain) · logging · clock · platform     │
 │                                                                                              │
 │  Hosting modes:  in-process (CLI, TUI child, ACP --local)  │  gateway daemon per profile      │
 │                  (P17: owns SessionService, cron ticker, messaging, control socket, WS)       │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘
      │                         │                          │                         │
      ▼                         ▼                          ▼                         ▼
 ┌───────────────┐   ┌──────────────────────┐   ┌───────────────────────┐   ┌─────────────────┐
 │ Model APIs    │   │ Terminal backends    │   │ External services     │   │ Local files     │
 │ (HTTPS, SSE)  │   │ local · docker · ssh │   │ web search/extract,   │   │ ~/.indus/<prof> │
 │               │   │ singularity · modal ·│   │ browser, TTS/STT,     │   │ config, .env,   │
 │               │   │ daytona · vercel     │   │ MCP servers, platform │   │ store, skills,  │
 │               │   │                      │   │ bot APIs              │   │ memory, logs    │
 └───────────────┘   └──────────────────────┘   └───────────────────────┘   └─────────────────┘
```

## 2. Design rules

1. **One runtime, many surfaces.** Only the Python runtime calls models, runs tools or writes the
   store. Surfaces present and answer questions; they never hold agent logic. Every surface reaches
   the runtime through `SessionService`, either in-process or across a transport, so adding a
   surface never forks behaviour.
2. **One declared contract.** Every RPC method, server request, event and error code is a Pydantic
   model in `indus.protocol`. JSON Schema and TypeScript types are generated from it; CI fails on
   stale output. Unknown params are rejected; error codes have one meaning (D-16.27).
3. **Persist before effect.** The user row is written before the first provider call; an assistant
   tool-call row before any tool side effect; a delivery-ledger row before any platform send; a
   cron attempt row before dispatch. Recovery code reads these rows and never replays work whose
   outcome is unknown.
4. **Scope is a value, never ambient.** A `Scope` (profile home, config view, secrets view,
   terminal policy) is passed to every call and copied into every background task. No module reads
   the home or env at import time. Under an unresolved scope we refuse, not fall back (R02.8,
   R03.23, R05.14).
5. **Adapters absorb vendor quirks.** Provider transports, terminal backends, messaging adapters,
   memory providers and MCP servers sit behind small contracts with fake implementations used in
   tests. The core loop never branches on a vendor name.
6. **Fail closed at trust boundaries.** Approval floors before every bypass, default-deny
   messaging auth, auth on every non-loopback listener and every WS upgrade, pre-tool hooks block
   on timeout, desktop IPC sender checks, no URL opening from untrusted content.

Import direction (enforced by an import-linter contract in CI):
`platform → foundation → protocol, state → providers, safety → backends → tools → core →
learning, plugins, mcp, cron → service → cli, tui_bridge, gateway, web, acp, mcp_serve, research`.
A module may import only from its left. `tools` sits above `backends` (D-04.7): the terminal,
process and file tools run through the backend contract, and no backend imports a tool.

## 3. Repo layout

```
indus-agent/
├─ pyproject.toml, uv.lock, .python-version         # one interpreter line (D-04 #1)
├─ src/indus/
│  ├─ platform/        host facts, OS paths, command locate (side-effect free; R09.17–18)
│  ├─ foundation/      home + profiles, Scope, config (load/save/schema/defaults), secrets
│  │                   (keychain + 0600 fallback), .env io, logging + redaction, clock, yamlio, ids
│  ├─ protocol/        Pydantic contract: methods, events, server requests, error table; codegen
│  ├─ state/           SQLite store: connections, schema, sessions, messages, FTS, titles, usage,
│  │                   compression records, ledgers, maintenance, export/import, integrity
│  ├─ providers/       profiles registry, transports/, auth/ (keys, device code, PKCE),
│  │                   credential pools, model catalog + context lengths, pricing
│  ├─ safety/          approvals (floors, patterns, modes, allowlists, smart judge), redaction,
│  │                   child-env builder, SSRF/url policy, path guards, threat scan
│  ├─ tools/           registry, toolsets, dispatch, result budget + spill, builtin/ (file, web,
│  │                   todo, clarify, process, code, delegate, browser/, media/, computer_use/)
│  ├─ backends/        execution contract, local, docker, ssh, singularity, modal, daytona,
│  │                   vercel; process registry; remote file sync; checkpoints
│  ├─ core/            Agent, turn loop + phases, prompt builder, context files, request
│  │                   assembly, retry/fallback, compression, usage, interrupts, subagents, MoA
│  ├─ learning/        memory (notes + profile stores, providers API), skills (format, index,
│  │                   loader, manage tool, hub), review fork, curator, journey
│  ├─ plugins/         manifest, discovery, allow-list, register(ctx), hooks bus, install/scan
│  ├─ mcp/             MCP client: config, connect (stdio/http), naming, trust, reload
│  ├─ cron/            grammar, job store, scheduler/ticker, runner, delivery targets
│  ├─ service/         SessionService, session registry, server-request broker, event fan-out,
│  │                   turn lease (P17), in-process host
│  ├─ cli/             click app (lazy subcommands), REPL, slash registry, wizards, pickers
│  ├─ tui_bridge/      JSON-RPC server over stdio/WS for the TUI (thin: maps protocol → service)
│  ├─ gateway/         daemon, control socket, messaging core (authz, pairing, session keys,
│  │                   busy guards, chunker, streaming, delivery ledger), adapters/<platform>/
│  ├─ web/             dashboard server (FastAPI): REST, WS (rpc, pty, events), auth, static SPA
│  ├─ acp/             ACP adapter (attach + --local modes)
│  ├─ mcp_serve/       MCP server mode (conversation/message tools)
│  └─ research/        batch runner, distributions, trajectory converter, compressor, evals
├─ apps/
│  ├─ tui/             @indus/tui — React + upstream Ink renderer
│  ├─ web/             @indus/web — Vite React SPA for the dashboard
│  └─ desktop/         @indus/desktop — Electron main, preload bridge, renderer
├─ packages/
│  ├─ protocol/        @indus/protocol — GENERATED TS types + schema (never hand-edited)
│  ├─ client/          @indus/client — JSON-RPC channel (stdio/WS), reconnect, heartbeat,
│  │                   event buffer, server-request handling; shared by tui/web/desktop
│  ├─ ui/              @indus/ui — design tokens, React components for web + desktop
│  └─ i18n/            @indus/i18n — catalogue loader shared by TS apps
├─ locales/            YAML catalogues (en first) + key-parity check
├─ skills/             our bundled starter skills (authored fresh)
├─ docker/  nix/  scripts/ (install.sh, install.ps1, release tooling)
├─ tests/              python: unit/, contract/, integration/, e2e/ (+ fakes/: model, platform, backend)
└─ docs/               plan/, notes/Pnn/, internals/ (acceptance map, traps ledger), user/
```

**Why a Python-defined protocol generating `packages/protocol` (D-04.3).** The runtime is the
authority and most methods are implemented in Python, so the contract lives next to the handlers
and validates their payloads at runtime. Pydantic emits JSON Schema; a generator script turns that
into TS types and a method/event catalogue. Three TS apps then share one source without anyone
writing types by hand. The alternative (TS-first schema with zod, Python generated) puts the source
of truth away from the code that must honour it, and Python codegen from zod is weaker.

**Why one `@indus/client` package.** The reference's renderer-side traps (event buffering before
mount, heartbeat counting any frame, rejecting in-flight requests on transport swap, unanswerable
server requests replied immediately; R05.2, R05.3, R05.9) apply equally to TUI, web and desktop.
Solving them once avoids three slightly different bugs.

## 4. Stack

| # | Layer | Choice | Why (our reasoning) | Alternative |
|---|---|---|---|---|
| 1 | Python version | **3.13 floor, 3.14 default** (`requires-python >=3.13,<3.15`; installer provisions 3.14 via uv) | 3.13 is in current distro releases and Docker base images; 3.14 is a year old by now and is what we test first. One line, no bridge markers (R10.12) | 3.12 floor for wider reach, at the cost of older typing and asyncio features |
| 2 | Packaging / env | **uv** (lock, sync, managed Python), hatchling build backend, src layout, optional extras per backend/platform | Fast, reproducible lock with `exclude-newer`; manages interpreters for the installer; extras keep core light | pip-tools + pyenv |
| 3 | Async model | **asyncio-first runtime** (D-04.1): the turn loop, providers, gateway, service and RPC servers are `async`; blocking work (subprocess waits, file IO, sync plugin handlers, SDK calls) runs in a bounded thread pool through one helper that adds deadlines and abandonment | One cancellation model for interrupts, timeouts and stream stale detection; the gateway already must be asyncio; many concurrent sessions in one daemon without a thread per turn. The reference runs the loop on threads and needs abandoned workers and socket shutdown from foreign threads (R01.4) | Thread-per-turn synchronous loop as in the reference (the D-04.1 fallback) |
| 4 | HTTP client | **httpx** (async, HTTP/2 optional, streaming), our own SSE parser | One client for providers, web tools, OAuth and webhooks; explicit connect/read timeouts; we can close a stream on interrupt deterministically | aiohttp client |
| 5 | Provider access | **Raw HTTP transports we own** for Chat Completions, Anthropic Messages, Responses, Gemini; **boto3/botocore** only for Bedrock signing (extra); no vendor LLM SDKs in core | Streaming stale detection, partial-stream stubs, retry classification and cache-marker placement need full control (R01.5, R01.6); SDKs add retries and timeouts we would have to fight; fewer deps | Official `openai`/`anthropic` SDKs with retries disabled |
| 6 | HTTP/WS server | **Starlette via FastAPI + uvicorn** for dashboard, API server, webhooks, daemon WS | Same Pydantic models as the protocol; ASGI WebSockets; mature middleware ordering for the auth gates (R09.4) | aiohttp server |
| 7 | Data models / validation | **Pydantic v2** for protocol, config schema, plugin manifests, cron jobs | Runtime validation plus JSON Schema export for codegen and the dashboard config form | msgspec (faster, weaker schema story) |
| 8 | SQLite access | **stdlib `sqlite3`** with our own thin repository layer: one writer connection per path (`BEGIN IMMEDIATE`, time-budgeted jittered retry), bounded read-only pool, WAL, FTS5; writes go through a single async-safe writer executor | No ORM magic between us and pragmas, checkpoints and FTS triggers that the traps depend on (R03.1–R03.18). uv's managed Python ships FTS5; `doctor` checks it | SQLAlchemy Core; `apsw` if a platform lacks FTS5 |
| 9 | YAML | **ruamel.yaml**, pure-Python, YAML 1.1 mode for reads, round-trip mode for every config write through one writer seam | Comment-preserving round trips are required (R04.5); one library for read and write avoids parser disagreement; pure Python avoids C-extension divergence | PyYAML for reads + ruamel for writes |
| 10 | CLI framework | **click** with a lazy-loading command group (D-04.4); profile pre-parse done by hand before any import | Nested groups for ~70 commands, optional-value options (`-c [NAME]`) without argparse routing hacks (R04.21), lazy imports keep `indus --version` fast, `CliRunner` for tests | argparse (the reference's choice, needs the speculative-parse workaround); Typer (slower import, more magic) |
| 11 | Classic REPL | **prompt_toolkit** (input, completion, key bindings) + **rich** (markdown, panels, live status) | Mature multiline editing and completion; rich renders streaming markdown well | Textual (a second full TUI we do not need) |
| 12 | Secrets | **keyring** for OS keychains (macOS Keychain, Windows Credential Manager, Secret Service) behind our `SecretStore`; 0600 file fallback; prompt-free availability probe with a timeout (D-16.13) | P4 default; one interface for CLI, gateway, desktop backend | 0600 files only (reference default) |
| 13 | Logging | stdlib `logging` with a queue handler, rotating files (`agent.log`, `errors.log`, `gateway.log`), JSON-lines option, redaction filter, session/profile tags from context vars | No extra dependency; redaction at one choke point; records routed by the record's scope (R03.23) | structlog |
| 14 | Messaging libraries | python-telegram-bot, discord.py, slack-bolt (Socket Mode); other platforms via their SDKs or raw HTTP, each in its own extra | Proven libraries for the first three; extras keep installs small | Raw HTTP everywhere (more work, fewer deps) |
| 15 | MCP | Official **MCP Python SDK** (client and server) | Spec conformance, OAuth helpers, stdio + streamable HTTP | Own implementation |
| 16 | ACP | Official **ACP Python SDK** at a pinned version, checked against the vendor schema | Third-party protocol; follow the vendor | Own JSON-RPC implementation from the schema |
| 17 | Browser tool | **Playwright (Python)** driving Chromium, with optional CDP attach | Same driver story as our E2E tests; persistent profiles; screenshot + accessibility tree | Selenium; a Node driver in a sidecar |
| 18 | PTY | **ptyprocess** on POSIX, **pywinpty** (ConPTY) on Windows | Needed for background processes and the dashboard chat tab | pexpect |
| 19 | TUI | **Node 24 LTS (24.21.0 in `.nvmrc`), TypeScript 6.0.3 strict, React 19, upstream Ink** (D-04.2), state in **zustand** stores, our own small modules for terminal modes, sync-output gating, kitty keyboard and mouse | Keeps the reference's runtime split (spec 05) without forking a renderer; zustand works the same in Ink, web and desktop | Our own diff renderer (D-04.2 fallback) |
| 20 | Web dashboard | **Vite + React 19 + TypeScript**, **Tailwind 4** with our tokens, **Radix** primitives, **TanStack Query**, React Router, **xterm.js** (fit, unicode11, web-links, webgl) for the chat tab | Matches the "admin SPA + embedded real TUI" design (R09.1); Radix gives accessible primitives under our own styling | SvelteKit (would not share components with desktop) |
| 21 | Desktop | **Electron** (current stable), **electron-builder**, Vite for main/preload/renderer, the same React/Tailwind/Radix stack and `@indus/ui`, **node-pty** + xterm for the terminal pane, electron-updater | The reference's runtime choice (spec 08), largest shared surface with the web app; mature signing and update tooling | Tauri (smaller, but no shared Node main-process code and a different security model) |
| 22 | Python ↔ TS RPC | **JSON-RPC 2.0**; NDJSON over stdio for the TUI child (P12) and internal children; **WebSocket** for daemon, web, desktop (P17+); single-use tickets passed as a WS subprotocol; REST for request-shaped dashboard data | One framing-agnostic contract; stdio needs no ports or auth for a child we spawned; WS for anything that outlives a process | gRPC (heavy for browsers and Ink); plain REST + SSE (no server→client requests) |
| 23 | JS workspace | **pnpm** workspaces, Node 24 LTS pinned in `.nvmrc`, TypeScript 6.0.3 (exact pins in 00-layout §4), lockfile with age window | Strict dependency isolation, fast installs | npm workspaces |
| 24 | Python tests | **pytest**, **pytest-asyncio** (strict mode), **hypothesis** for grammars/chunkers/parsers, golden files for prompts and tool arrays, fakes for model/platform/backend; per-file subprocess isolation in CI (R10.20) | Matches the trap list: table tests for arithmetic, property tests for chunking and cron, golden prompts for cache stability (R01.1) | unittest |
| 25 | TS tests | **vitest** + **ink-testing-library** (TUI), **Testing Library** (web/desktop renderer), **Playwright** for web E2E and Electron E2E | One runner for unit tests across apps; Playwright drives both browser and Electron | Jest; WebdriverIO for Electron |
| 26 | Lint / format / types | **ruff** (lint + format, one config in `pyproject.toml`), **pyright** (strict on core, foundation, protocol, state, service); **Biome** (lint + format) + `tsc --noEmit` for TS; import-linter for layer rules | One config per language, one target version (R10.13); Biome is fast and replaces eslint+prettier | mypy; eslint + prettier |
| 27 | CI | GitHub Actions: change classifier (fail-open), Python slices, TS jobs, E2E lanes, clean-room guards (banned identifiers, n-gram overlap), lock and pin checks, one aggregate gate where skipped counts as failed (R10.17–R10.19) | Mirrors the reference's hard-won CI lessons in our own pipeline | — |

## 5. Runtime modes

| Mode | Command | Who hosts `SessionService` | Arrives |
|---|---|---|---|
| In-process REPL | `indus` / `indus chat` | the CLI process itself | P06 |
| One-shot | `indus -z "…"`, `indus chat -q "…"` | the CLI process; exits with documented codes (R04.20) | P06 |
| TUI, child mode | `indus --tui` | a Python child `indus tui-bridge --stdio` spawned by the Node renderer | P12 |
| Gateway daemon | `indus gateway run` (foreground) or as a user service | the daemon (one per profile, P8): service, cron ticker, messaging adapters, control socket, WS endpoint | P15 (messaging), P17 (shared backend) |
| Attached surfaces | `indus`, `indus --tui`, desktop, `indus acp` once P17 lands | the daemon; surfaces attach with a single-use ticket; `--local` keeps in-process mode | P17 |
| Dashboard | `indus dashboard` | the web server attaches to the daemon (starts it if needed) | P18 |
| ACP | `indus acp` | attach (default after P17) or `--local` | P19 |
| MCP server | `indus mcp serve` | none: reads the store read-only, sends through the delivery library | P19 |
| Batch / research | `indus research batch …` | one in-process agent per worker in a process pool | P22 |

Before P15, cron runs through `indus cron tick` (called by a user timer or the `indus cron daemon`
loop) with local delivery; from P15 the ticker moves into the gateway daemon, which is
also where the reference runs it.

Before P17, two in-process surfaces could open the same session. A per-session advisory file lock
in `state/` prevents concurrent turns (D-16.69); P17 replaces it with the durable turn lease.

## 6. End-to-end sequence: one message in the classic CLI

```
User        CLI REPL        SessionService     Agent loop (core)      Store        Provider       Tools/safety      Backend
 │  "fix the test" │               │                   │                 │              │                 │               │
 │────────────────►│ submit(text)  │                   │                 │              │                 │               │
 │                 │──────────────►│ admit turn        │                 │              │                 │               │
 │                 │               │ (session lock)    │                 │              │                 │               │
 │                 │               │──run_turn────────►│ prologue:       │              │                 │               │
 │                 │               │                   │ restore prompt ►│ read prompt  │                 │               │
 │                 │               │                   │ append user row►│ COMMIT       │                 │               │
 │                 │               │                   │ build request (wire copy, cache markers)        │               │
 │                 │               │                   │────────── stream request ─────►│                 │               │
 │◄── delta ───────│◄── event ─────│◄── on_delta ──────│◄───────── SSE deltas ──────────│                 │               │
 │                 │               │                   │ response: tool_call terminal(rm build/)         │               │
 │                 │               │                   │ append assistant tool-call row►│ COMMIT        │               │
 │                 │               │                   │──────── dispatch(terminal, args) ──────────────►│ floors, deny  │
 │                 │               │                   │                 │              │ globs, patterns │               │
 │                 │               │◄── server request: approval(cmd redacted) ─────────────────────────│ → "dangerous" │
 │◄─ approval card │◄──────────────│                   │                 │              │                 │               │
 │── "allow once" ►│──────────────►│ answer request ───────────────────────────────────────────────────►│ approved      │
 │                 │               │                   │                 │              │                 │── execute ───►│
 │                 │               │                   │                 │              │                 │◄─ rc, output ─│
 │                 │               │                   │◄──────── result string (redacted, capped, spilled if large) ────│
 │                 │               │                   │ append tool row►│ COMMIT       │                 │               │
 │                 │               │                   │────────── next iteration ─────►│                 │               │
 │◄── delta ───────│◄── event ─────│◄── on_delta ──────│◄───────── final text ──────────│                 │               │
 │                 │               │                   │ finalize: append assistant row, usage, title ►│ COMMIT        │
 │                 │               │                   │ post-turn hooks; review counters (learning)    │               │
 │◄─ done + usage ─│◄── turn.done ─│◄── result ────────│                 │              │                 │               │
```

Interrupt path: Ctrl+C in the REPL calls `SessionService.interrupt(session, generation)`; the
service cancels the turn task; the provider stream is closed; a pending approval is withdrawn
(one cancel event); the loop's single exit funnel appends synthetic tool results for any
unanswered tool calls, persists the sealed partial text, and emits `turn.done{reason:
interrupted}` (R01.2, R01.4, R01.18).

## 7. Cross-cutting concerns

- **Config.** One loader, one defaults table generated from the Pydantic config schema (D-16.32),
  deep merge user file over defaults, `${VAR}` expansion, last-known-good on parse failure, writes
  only through the round-trip writer with atomic replace and default stripping, fail closed on an
  unreadable file (R04.3–R04.8). Config is read through the `Scope`, cached by file signature.
- **Secrets.** `SecretStore` per scope: keychain first, 0600 file fallback (P4); `.env` kept for
  env-shaped settings and for users who prefer files; env writer denylist (R04.12); child processes
  receive environments only from the single env builder in `safety/` (R02.22); redaction applied to
  tool output, logs, `config get`, approval cards.
- **Logging.** Queue-based handlers, per-profile files, redaction filter, session id and turn id in
  every record, stderr-only logging in stdio protocol modes from the first import (R09.14),
  `indus logs` to tail and filter.
- **Profiles.** Resolved before any app import (`-p`, sticky active profile, `INDUS_HOME`), carried
  as `Scope` everywhere after that; profile ids follow one rule; deletes leave tombstones (R04.25);
  the gateway serves one profile per process until P24.
- **Error handling.** Library code raises typed errors (`IndusError` tree with stable codes);
  boundaries convert them: tools return a bounded error string to the model, the service maps them
  to protocol error codes, the CLI maps them to exit codes and a short user message plus a log
  pointer. No raw tracebacks to models or users.
- **Cancellation and time.** Every turn is an asyncio task with a generation number; interrupts and
  redirects name the generation they target. Every blocking call runs under a deadline with an outer
  backstop (timeout + 2 s for commands, R02.3) and is abandoned, never joined, on cancel. Retries are
  deadline-based with jitter (R03.8). The clock is injectable for tests; no real sleeps in tests.
- **Concurrency in the store.** One writer per database file per process, serialized through an
  executor; readers bounded; all write callbacks idempotent; FTS treated as derived and never
  rebuilt inline (R03.6).
- **Security posture.** Approval floors before any bypass, isolated-backend detection by mounts not
  names (R02.6), SSRF checks on URL tools, media path policy on outbound files (R06.22), plugin and
  hub scanners, dashboard auth gates, desktop IPC sender checks and CSP (D-16.18–D-16.20).
- **Observability.** Usage and cost per session in the store; `indus doctor` checks environment,
  keys, store health, FTS5, backends; optional OpenTelemetry export as an extra later.

## 8. Decisions

| ID | Decision | Trial | Fallback | Status |
|---|---|---|---|---|
| D-04.1 | **asyncio-first runtime** (agent loop, providers, service, gateway all async; blocking work in a bounded pool with deadlines). **This is the riskiest choice.** It differs from the reference's thread-based loop, and if it is wrong, every later phase pays | 1-week trial in P00–P03 (`docs/notes/P00/async-trial.md`): with a fake streaming provider, prove (a) interrupt mid-stream closes the stream within 200 ms and leaves alternation valid; (b) a tool command with timeout is killed at timeout + 2 s while the loop stays responsive; (c) 50 concurrent fake sessions in one loop keep p99 event latency < 50 ms; (d) a synchronous plugin hook that hangs is abandoned after its timeout without blocking other sessions; (e) a sync caller (classic REPL key handler) can drive and interrupt a turn through a loop thread. All five must pass | Thread-per-turn synchronous core behind the same `SessionService` and transport interfaces (the reference's model): agent loop sync, providers on httpx sync client, interrupts via flags + stream close. Only `core/` and `providers/` change; gateway and servers stay async and run turns in threads | Lead, 2026-10-18 — **decided (P04-23): asyncio core kept**; criteria (a)–(d) on the real loop in `docs/notes/P04/d-04-1-decision.md`, (e) in `tests/integration/core/test_d04_1_sync_caller.py` |
| D-04.2 | **Upstream Ink** for the TUI, no fork | 4-day spike on the frontend track before P12: alt screen, kitty keyboard, bracketed paste, mouse + selection, DEC 2026 gating outside tmux, 10k-token streaming at ≥ 30 fps, resize storm coalescing (R05.16, R05.17) | Keep upstream Ink for layout and wrap our own output writer (sync-output and scroll-region fast path) around it; a full custom renderer only if the spike fails twice | Lead, 2026-10-18 — spike pending |
| D-04.3 | Protocol defined in Python (Pydantic), TS generated into `packages/protocol` | P04 task builds the generator and a stale check; first consumer is the TUI mock in the frontend track | Hand-written JSON Schema files as the source, generating both Python and TS | Lead, 2026-10-18 |
| D-04.4 | click instead of argparse for the CLI | P06/P07 proves `-p` pre-parse, bare-command chat, `-c [NAME]`, multi-word names, exit codes, `--version` < 150 ms | argparse with our own routing helper | Lead, 2026-10-18 |
| D-04.5 | Raw HTTP provider transports, no vendor LLM SDKs in core | P03 records two live captures per transport and replays them in tests | Vendor SDK per transport, retries off, wrapped in our stream handling | Lead, 2026-10-18 |
| D-04.6 | stdlib `sqlite3` with our own repository layer | P02 stress test: 4 processes × 8 threads appending for 60 s with a VACUUM in the middle, no lost rows, no `database is locked` surfaced | apsw for finer lock and checkpoint control | Lead, 2026-10-18 |
| D-04.7 | **`indus.tools` is a layer above `indus.backends`** (was one layer with the two independent). The terminal and `process_manage` tools drive `EnvironmentRegistry`, `ProcessRegistry` and the sudo hook directly; the file tools may import the backend contract instead of restating it. Backends never import tools | import-linter `Layer order` contract (`pyproject.toml`) and `tests/unit/test_layers.py` pin the order; P05 wave 2 builds the terminal (P05-11/12) and process tools (P05-13/14) against it | Keep one shared layer and pass every backend object to tools through structural protocols and `ToolContext.extras` only | Lead, 2026-10-18 — decided at the P05 wave 2 merge |
