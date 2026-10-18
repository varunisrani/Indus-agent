# 16 · Same product, our own build: what stays and what changes

This page fixes what "same" and "different" mean for Indus Agent, so no builder has to guess. It is
written from the ten research specs in `research/` and the decisions in `README.md` (P1–P8 and the
lead defaults). Names follow `17-naming-and-glossary.md`; feature areas follow
`03-feature-inventory.md` (cited by area name here, because ids are assigned in parallel).

Working name: **Indus Agent**. CLI `indus`, env prefix `INDUS_`, home `~/.indus`, Python package
`indus`, npm scope `@indus/*`. The name is confirmed for the build but may still change before
launch after a trademark check (D-16.40, owner).

D-ids: `D-16.n` for this doc, `D-04.n` for stack decisions in `04-architecture.md`, `D-15.n` for
roadmap decisions. Every intended difference found while building or in the parity pass gets a row
in §2.7 (or §2.8 if it is a gap) and is cited by the parity ledger and the acceptance map. A
difference with no id is a bug or an open question. Agents propose, the owner or lead decides, and
the date is recorded in §3.

---

## 1. What stays the same (we deliberately match the reference)

| Area | Stays the same |
|---|---|
| **Product idea** | A self-improving personal agent: one runtime that chats, runs tools on a chosen machine, remembers, writes its own skills, schedules work, and is reachable from a terminal, an editor, a browser, a desktop app and messaging apps. |
| **Architecture** | Python owns all agent behaviour (turn loop, providers, tools, state, learning, cron, gateway). TypeScript front ends (TUI, web, desktop) own only presentation and talk to Python over one declared JSON-RPC contract. One SQLite store per profile is the source of truth for conversations. A per-profile gateway process is, in the end, the single owner of execution for every surface (P1 says when). See `04-architecture.md`. |
| **Information architecture** | Terminal: transcript above, live work dock, status rule, multiline composer, modal cards for approvals and pickers. Desktop: left sidebar (sessions, projects, profile rail), central tabbed panes, optional right panes (preview, files, git, terminal), bottom status bar, route overlays. Web: left navigation of admin pages, a chat page that embeds the real TUI in a browser terminal with a narrow side rail. Messaging: one conversation slot per chat, slash commands, inline approve/deny. |
| **Core flows** | Install → `indus setup` (provider, key, model, backend, tools) → `indus` opens a chat → the agent streams text, calls tools, asks before risky commands → the session is saved and can be resumed, searched, titled, branched, compressed → memory and skills grow between sessions → cron jobs and messaging bots reuse the same agent. |
| **Feel** | Keyboard-first and dense in the terminal; streaming from the first token; interrupt, steer and queue always available; nothing lost on crash (user message persisted before any model call, replies ledgered before delivery); cheap long sessions (byte-stable system prompt for provider caches). |
| **Behavioural contracts** | Strict role alternation and one tool result per tool call on every failure path; persist-before-effect; approval floors that no mode can bypass; default-deny messaging authorization; at-least-once delivery with ambiguity labelled; at-most-once cron firing; curator archives and never deletes; frozen memory snapshot per session. |
| **Third-party protocols** | Kept exact where they are someone else's standard: OpenAI Chat Completions and Responses, Anthropic Messages, Bedrock Converse, Gemini, MCP, ACP, JSON-RPC 2.0, SSE, OAuth device code (RFC 8628) and PKCE, `SKILL.md` frontmatter as published by agentskills.io, platform bot APIs. |
| **Cron grammar** | The reference grammar is kept on purpose (P6): a bare duration repeats, `in <dur>` runs once, cron expressions and ISO timestamps also accepted. |

## 2. What changes (on purpose)

### 2.1 Identity: required, non-negotiable

| Reference | Indus Agent |
|---|---|
| Product name, CLI, Python package, console scripts, plugin entry-point group, user agent string | Our own: `indus`, package `indus`, entry-point group `indus.plugins`, UA `indus/<version>` (full map in `17-naming-and-glossary.md`) |
| Home dir, env prefix, file names (persona file, project context file, store file, auth file) | `~/.indus`, `INDUS_*`; persona file `PERSONA.md`, project file `INDUS.md` / `.indus.md`; our own store and auth file names. Third-party convention files (`AGENTS.md`, `CLAUDE.md`, cursor rules) are still read because they belong to other tools, not to the reference. |
| Toolset names, metric names, deep-link scheme, IPC prefix, bridge global, HTTP headers | Our own (`indus-cli` style toolsets, `indus://` scheme, our own channel prefix and header names) |
| Logo, icons, favicons, sprites, sounds, desktop pet art | Made or licensed by us |
| Palette, theme tokens, theme and skin names, fonts, UI kit | Our own design system (`packages/ui`); OFL fonts; no vendor UI-kit package, no vendor fonts or theme preset names (R09.22) |
| All prompt text the model sees (identity, guidance blocks, nudges, summaries, cron hint) | Written fresh; same purpose, our words (lead default) |
| All UI copy, onboarding, help, errors, playful strings (spinner verbs, faces, fortunes), pairing replies | Written fresh in our own voice (lead default) |
| Bundled and optional skills, plugin examples, locale catalogues | Authored by us from scratch (D-16.33); none copied or translated from the reference |
| Contributor attribution data | Not copied; starts empty (R10.29) |

### 2.2 Protocol and storage: own names, not wire- or DB-compatible

**Decision D-16.1:** Indus Agent is **not wire-compatible** with the reference. We name our own
JSON-RPC methods, events, server-request kinds, error codes, REST routes, WebSocket paths,
control-socket verbs, IPC channels, ticket formats and ID prefixes. A reference client cannot talk
to our backend and the reverse. Why: matching would force us to mirror the reference's concrete
schemas (its expression, not its ideas), would import its known flaws (error codes that mean
different things per method, R05.15), and would tie our release cadence to theirs.

**Decision D-16.2:** Indus Agent is **not storage-compatible** with the reference. Our SQLite store
has its own table, column, index and trigger names, its own message-content codec and its own
FTS projection; our config keys, `.env` names (except vendor-standard platform credentials,
D-16.22), auth file layout, cron job store, skill usage sidecars, delivery ledger and spill layout
are our own. We do not open, read or migrate a reference home in 1.0. Why: compatibility would
pin us to the reference's column order and 30+ schema versions, and the clean-room rule is easier
to keep when no file format is shared.

Optional later (post-1.0, owner's call, D-16.41): a one-way importer that reads a *copy* of a
reference home (sessions JSONL export, memory files, skills) and writes our formats, built from a
field-mapping spec, never from reference code.

Our protocol rules (own design, recorded so builders do not drift):
- One contract, declared once in Python, with generated TypeScript types and a JSON Schema
  document; CI fails when generated files are stale (D-04.3).
- Error codes have one meaning across all methods, listed in one table (D-16.27).
- The same method names serve every transport (stdio NDJSON for the TUI child and ACP-internal use,
  WebSocket for the daemon, desktop and dashboard).

### 2.3 Fresh start, so no legacy baggage

We start on the reference's *final* behaviour and skip every path that exists only to carry old
installs forward:

- **Config migrations.** The reference stamps `config.yaml` with a version (current 51, support
  floor 12) and runs ordered migration steps. We start our stamp at 1 with an empty migration list;
  the hook exists so our own later changes can use it. Legacy key moves (root-level turn cap,
  single-fallback key, list-shaped custom providers, old "systems" shape) are not read.
- **Retired-model and exporter migrations** (`migrate` subcommands for retired vendor models, a
  relay exporter env family, a runtime switch for one vendor) are not built.
- **Store schema history.** The reference's schema has ~32 versions, a storage-layout upgrade path
  and chunked data migrations. Our store starts at schema 1 on the final layout, with declarative
  column reconcile from day one (new columns need no version bump) and chunked, time-boxed
  migrations only for our own future changes (R03.15 still applies to us).
- **Old updater surface.** The reference keeps a stable import surface so an old updater can
  replace itself (R04.24) and an interpreter-bridge marker so installs on older Pythons can reach
  their updater (R10.12). We require one interpreter line (D-04 stack) and run the updater from the
  installed release, so neither trick is needed. Post-pull syntax check and rollback stay.
- **Legacy entry points.** No second "legacy single-query runner" console script; one `indus` CLI
  plus `indus acp` as a subcommand (a thin `indus-acp` shim only if editor registries need a
  separate binary).
- **Two-generation transports.** The reference TUI carries a legacy stdio backend next to a newer
  shared-daemon WebSocket path (R05.1), and ACP carries a legacy in-process class next to a gateway
  viewer (R09.13). We build each mode once, explicitly, behind one interface (D-16.10, D-16.29).
- **Imports from other products.** The OpenClaw import (`claw migrate`) is not built (D-16.50).
  Importing another coding agent's instruction and permission files stays in scope as a feature
  (P24), not as a migration.
- **Small compat paths skipped:** legacy checkpoint store cleanup, legacy comment-only config
  scaffold upgrade, legacy sibling-backup sweeps, label-based lookup of old Daytona sandboxes,
  profile identity migration, the empty compatibility extras in packaging, "nightly" channel naming
  (we say canary everywhere, R10.27), one-time desktop secret-store migration.
- **Capability negotiation** (`client.capabilities`, server feature flags) exists from P04 but
  starts empty; every flag added later is ours.

### 2.4 Scope: full parity, in phase order

The lead set **full parity** as the 1.0 target. "v1" below means "in 1.0", with the phase that
delivers it (`15-roadmap.md`). "Later" means after 1.0 or still open with the owner.

| Feature area | Reference | Indus 1.0 | Later |
|---|---|---|---|
| Agent turn loop, prompt assembly, steer/redirect/interrupt | Full | **Full** (P04, P08) | — |
| Model providers | 42 bundled profiles, 5 API modes, OAuth routes incl. impersonating ones, vendor portal | **API-key and documented OAuth/device-code providers**, Chat Completions + Anthropic Messages (P03), Responses + Bedrock + Gemini native (P08), credential pools, fallback chain (P08). Bundled list per D-16.42. | Subprocess "external agent" transport (P24 if owner agrees) |
| Subscription OAuth that impersonates another client | Yes | **No** (D-16.11) | — |
| First-party hosted portal provider, free tier, top-up, subscription commands | Yes | **No** (D-16.12) | — |
| Context compression, prompt caching, usage/cost | Full | **Full** (P08) | — |
| Mixture-of-agents, side questions, fast mode, goals/loops/heartbeat | Yes | **Yes** (P08 fast mode; P10 MoA, side questions, goals/loops) | — |
| Core tools: file, terminal, process, web, todo, clarify | Yes | **Yes** (P05) | — |
| Delegation, code execution, browser, vision, image/video gen, TTS/STT, voice mode | Yes | **Yes** (P10) | — |
| Computer use, LSP, worktrees, verify | Yes | **Yes** (P10 computer use; P11 worktrees; P24 LSP, verify) | — |
| Terminal backends | 7 + managed Modal gateway | **Local, Docker, SSH** (P05/P11); **Singularity, Modal (direct), Daytona, Vercel Sandbox** (P21) | Managed Modal gateway: never (D-16.16) |
| Background processes, checkpoints/rollback | Yes | **Yes** (P11) | — |
| Approvals (manual, smart, yolo, allowlists, floors) | Yes | **Yes** (P05 manual + floors; P08 smart pre-judge) | — |
| Sessions store, FTS search, titles, usage, export/import, prune, repair | Yes | **Yes** (P02, repair/recover tools P07) | Reference-store importer (D-16.41) |
| Classic CLI REPL, slash commands, one-shot modes | Yes | **Yes** (P06) | — |
| CLI management (setup, model, auth, config, sessions, doctor, backup, logs, uninstall, update) | ~70 commands | **Yes** (P07; update and uninstall finished in P23) | — |
| Profiles | Yes, plus multiplexed serving | **Yes** (P01); multiplexed serving P24 | — |
| Memory, skills, curator, background review, learn/refine, learning journey | Yes | **Yes** (P09) | — |
| External memory providers | Plugin-shaped | **Yes** (P13, via plugins) | — |
| MCP client, plugins, hooks, skills hub | Yes | **Yes** (P13) | Curated plugin catalog, out-of-process plugin host (D-16.45, D-16.46) |
| Cron | Yes | **Yes** (P14; ticker moves into gateway in P15) | — |
| TUI | Ink fork, shared-daemon WS default | **Upstream Ink** (D-16.28), stdio child first (P12), daemon attach (P17) | — |
| Messaging gateway | ~30 adapters, API server, webhooks, relay, hosted rooms, multiplexing | **Telegram, Discord, Slack + API server + webhook** (P15); **all other chat adapters** (P16); multiplexing, hosted rooms, relay, A2A (P24) | — |
| Kanban board, workers, projects | Yes | **Yes** (P24) | — |
| Shared daemon as backend for every surface, turn lease, canonical wire | Yes (default) | **Yes** (P17, parity path) | — |
| Web dashboard | ~20 pages + embedded TUI chat | **Yes** (P18); login providers per D-16.44 | Dashboard plugins beyond the SDK basics (owner) |
| ACP adapter | Gateway viewer (+ legacy in-process class) | **Yes** (P19), one implementation per mode (D-16.29) | — |
| MCP server mode | 10 tools, two hollow | **8 working tools**; approval tools only once wired to the daemon (D-16.30) (P19) | — |
| Desktop app | Electron, huge | **Yes** (P20): chat, sidebar, panes, settings, onboarding, installer, updates, HUD, quick entry | Pet and wake word in P24; native setup installer and light remote-only variant per D-16.47/D-16.48 |
| Research tooling (batch, trajectories, compression, evals) | Yes | **Yes** (P22) | — |
| Packaging | Installer scripts, Docker, Nix, Termux APT, MSIX/Store, desktop bundles; no PyPI | **Installer (sh + PowerShell), Docker, Nix, desktop bundles** (P23) | Termux APT, MSIX/Store (P24 if capacity, else post-1.0; D-16.15) |
| Locales | 17 catalogues | **Mechanism + English + parity check** | Further languages (D-16.51) |
| Telemetry | Consent flow, install receipt | **None** (D-16.49) | Opt-in only if the owner asks |
| Docs website | Large, with a translation | **User guide + reference in English** (P25) | Translations |

### 2.5 Visual differences: siblings, not twins

Keep the **layout, density and interaction model** of every surface: the same regions in the same
places, the same keys for the same actions where the action is a convention (Ctrl+C order,
Esc-Esc, Enter vs Shift+Enter), the same card-based approvals.

Change the **surface**:
- **Palette.** Our own tokens on `:root` and in the TUI theme file: a warm clay/terracotta accent
  with deep teal secondary on neutral slate, not the reference's colours. Light and dark variants
  of each, contrast-checked (WCAG AA) in CI for web and desktop.
- **Type.** OFL fonts only (for example Inter for UI text and JetBrains Mono for code), bundled with
  the licence file.
- **Shape and motion.** Our own radius scale, elevation, focus rings and motion timings.
- **Iconography and art.** A licensed open icon set (for example Lucide, ISC) plus our own logo,
  empty-state art, tray icons, sprites. No reference images.
- **TUI.** Our own default theme and theme names, our own prompt glyph, banner and spinner set;
  same status-rule information at the same width breakpoints.
- **Words.** Every label longer than a word or two is rewritten; short conventional labels
  ("Save", "Cancel", "Settings") may coincide.

A parity screenshot comparison must show the same regions in the same places with clearly
different branding.

### 2.6 Small improvements we may add (owner's call)

1. Stable, documented error-code table across the protocol (adopted as D-16.27).
2. Batch runner checkpoints bound to a dataset hash (adopted as D-16.31).
3. Dashboard "effective config" view that prints where each value came from (default, file, env,
   admin overlay) — helps the reference's own docs-vs-defaults confusion (R10.4). Proposed for P18.

### 2.7 Intended differences

Seeded from the product decisions (P1–P8), the lead defaults and the security items the specs
raised. Builders add rows as task notes decide new ones; the "Decided in" column then names the
note (`docs/notes/Pnn/<slug>.md`).

| ID | Area | Reference | Indus 1.0 | Decided in |
|---|---|---|---|---|
| D-16.10 | Runtime / surfaces | Classic chat and TUI attach to a per-profile shared daemon by default; the REPL never runs the agent itself (R04.1, R05.1) | **In-process first** (P1): the classic CLI runs the agent in its own process; the TUI spawns a Python child over stdio. Both talk to one `SessionService` interface. The per-profile gateway daemon arrives in P17 and then becomes the default (parity path) for every surface, with in-process kept as `--local` | README P1 |
| D-16.11 | Providers / auth | Subscription OAuth routes that present another vendor's client identity, headers and renamed tools | **API keys and officially documented OAuth/device-code flows only** (P2). No identity lines, headers or tool renames claiming another product | README P2 |
| D-16.12 | Providers | First-party hosted "portal" inference provider with free tier, top-up and subscription commands | **Dropped** (P3); no portal provider, no account commands, no portal login on the dashboard | README P3 |
| D-16.13 | Secrets | `.env` and auth file on disk; desktop secrets plain 0600 by default, keychain opt-in | **OS keychain by default**, falling back to owner-only 0600 files under 0700 dirs (P4). Availability is probed without triggering interactive prompts; a keychain that errors or prompts is marked unusable for that run and we fall back (guards R08.10) | README P4 |
| D-16.14 | Distribution | Installer, Docker, Nix, Termux, MSIX; wheel builds blocked | **Same channels, no PyPI** (P5). Installer scripts, Docker, Nix in 1.0; Termux and MSIX later | README P5 |
| D-16.15 | Distribution | Termux APT and MSIX/Store in the main release flow | **Later**: built in P24 only if capacity allows, else post-1.0 | README P5 |
| D-16.16 | Terminal backends | Seven backends plus a vendor-hosted managed Modal gateway | **Local, Docker, SSH first; the other four later behind the same contract** (P7). Managed Modal gateway dropped; Modal runs in direct mode with the user's own credentials | README P7 |
| D-16.17 | Gateway | Multiplexed profiles on by default; hosted rooms | **One profile per gateway process** in the first gateway phases (P8); multiplexed profiles and hosted rooms in P24 | README P8 |
| D-16.18 | Desktop security | A few IPC handlers check the calling frame | **Every IPC handler checks its sender** (app origin, main frame, known window) and rejects otherwise | Lead default |
| D-16.19 | Desktop security | No CSP found for the chat renderer; fuses not configured in source | **Strict CSP** on every renderer page; **Electron fuses set** (no RunAsNode, no NODE_OPTIONS or inspect args, ASAR integrity, load only from ASAR, cookie encryption on) | Lead default |
| D-16.20 | Desktop / web security | Guest content mostly guarded; some paths reach the window-open handler (R08.8) | **Untrusted content can never open URLs**: deny-all window-open in guest frames, external opens only from a trusted user click, http/https only | Lead default |
| D-16.21 | Research tooling | Trajectory compressor loads its tokenizer with remote code enabled (R10.10) | **Remote code off by default**; an explicit flag opts in and prints a warning | Lead default |
| D-16.22 | Config / env | Platform credentials unprefixed (`TELEGRAM_BOT_TOKEN`) | **Same vendor-standard names** for platform credentials; every product variable uses `INDUS_` | Lead default |
| D-16.23 | Copy | Reference prompt text, UI copy, playful strings, bundled skills | **All written fresh**; nothing carried over | Lead default |
| D-16.24 | Docs vs code | The reference's docs and code disagree in many places | **We follow the code.** Known cases: project-context AGENTS chain walks git root → cwd; compression threshold arithmetic per code (R01.7); curator stale 14 d / archive 30 d / interval 168 h (R07.7); Docker PID limit 2048 and persistent containers left running for reuse (R02.23); delegation defaults 10 children / depth 1; process tool is one tool with actions; MCP tool names use the double-underscore prefix (R07.21); TUI transport per D-16.10 not per docs; approval quick-picks are digits (R05.11). Each builder records further cases here | Lead default |
| D-16.25 | Web / ACP auth | Non-loopback dashboard fails closed; an "insecure" flag lingers as a no-op; ACP viewer trusts the local socket | **Fail closed everywhere**: the dashboard refuses to start on a non-loopback bind or public URL without a login provider; there is no insecure flag at all; loopback tokens are rejected in gated mode; every WebSocket upgrade goes through one auth helper; ACP and every other local client attach to the daemon only with a single-use ticket over an owner-only socket | Specs R09.2, R09.3, R08.4 |
| D-16.26 | Gateway API server | Loopback API server may run without a key (open question 06 Q13) | **Proposed:** the OpenAI-compatible API server always requires a bearer key, loopback included; `indus gateway api-key` creates one | Open — lead (§3) |
| D-16.27 | Protocol | Error codes reused with different meanings per method (R05.15) | **One error-code table** with stable meanings, generated into TS | Spec R05.15 |
| D-16.28 | TUI | Heavily forked Ink renderer and a TS port of Yoga | **Upstream Ink** as a dependency; missing capabilities (synchronized output gating, kitty keyboard, mouse, selection) added as our own small modules around it. Trial and fallback in D-04.2 | Task brief / spec 05 |
| D-16.29 | ACP | One constructor silently returns either a gateway viewer or a legacy in-process agent; mode advertising inconsistent (R09.13) | **Two explicit modes**: `indus acp` attaches to the profile daemon (default once P17 lands) or runs `--local` in-process; both advertise the same session modes | Spec R09.13 |
| D-16.30 | MCP server mode | Two approval tools advertised but never populated (R09.10) | **Not advertised** until they are wired to the daemon's approval queue; then real | Spec R09.10 |
| D-16.31 | Research tooling | Checkpoint reuse by run name even when the dataset changed; tool names by position; two trajectory writers; timed-out samples silently dropped (R10.3, R10.6–R10.8) | Checkpoint **bound to a dataset hash**; tool responses matched **by call id** (position as fallback); **one shared converter**; a **rejects file** for dropped samples | Spec 10 |
| D-16.32 | Config | Three config loaders with different defaults (R06.25, 04 §3.11.1) | **One loader, one defaults table**, with a presence query ("did the user set this key?") for presence-sensitive readers | Spec R06.25 |
| D-16.33 | Skills | 58 bundled skills plus optional catalogue | **Our own starter set**, written fresh (size per D-16.43) | Lead default |
| D-16.34 | Packaging | Core deps carry an interpreter marker so old Pythons install with no deps (R10.12); lint config shadowed and versions mismatched (R10.13) | **One interpreter line, one lint/type config, one target version**; pinning policy enforced literally by CI (D-16.52) | Spec 10 |
| D-16.35 | Approvals | Reader falls back to manual while shipped config writes smart (02 Q13) | **One default from one table** (value per D-16.53) | Spec R02.23 |
| D-16.36 | Cron grammar | Bare duration repeats; `in <dur>` once (kept, P6). Doc example "every 1d at 09:00" may not parse | Kept; **"every Nd at HH:MM" is accepted** and means daily at that time (proposed, D-16.54) | README P6 |
| D-16.37 | Messaging tools | Delivery is a library, not a model tool (R02.26) | Same; recorded so no builder "adds" a send tool | Spec R02.26 |
| D-16.38 | Dashboard chat | Chat tab embeds the real TUI over PTY; on native Windows tries ConPTY then falls back to a banner (R09.19) | Embed the TUI the same way (R09.1). **Windows: ConPTY via the PTY library; if it cannot start, show one clear "use WSL2" notice**; documented once | Spec R09.19 |
| D-16.39 | Plugins | Hook names generic; entry-point group branded | Generic hook names kept (they describe behaviour); entry-point group `indus.plugins` | Spec 01 §2 |

### 2.8 Not in 1.0

Parts of P0/P1 inventory rows that 1.0 ships without. The acceptance map lists these under
"Known gaps". A row built later moves to §2.7.

| ID | Feature | Indus 1.0 | Why not in 1.0 |
|---|---|---|---|
| D-16.41 | Reading or importing a reference home (store, exports, memory, skills) | Not supported; users start fresh | D-16.2; clean-room simplicity. Optional one-way importer later (owner) |
| D-16.45 | Curated plugin catalog and optional-MCP catalog | Plugins install from a git URL with a full 40-char SHA pin or a local dir; MCP servers are configured by hand or via `indus mcp add` | A catalog is a governance process (review, hosting), not code (07 Q14). Owner decides if we run one |
| D-16.46 | Out-of-process plugin host | Plugins run in-process behind allow-list, static scan and consent (same trust model the reference documents as "not a sandbox") | Security posture call for the owner (07 Q12); recommended post-1.0 |
| D-16.47 | Separate native setup installer app | The desktop app's in-app installer covers first run | Second app to build, sign and ship for one flow (08 Q10) |
| D-16.48 | Light (remote-only) desktop variant | One desktop variant that can connect local or remote | Packaging matrix cost (08 Q11) |
| D-16.49 | Telemetry and install receipt upload | No telemetry; local install receipt only | Privacy default; nothing to upload to |
| D-16.50 | Import from another agent product's home (OpenClaw migration) | Not built | Migration tool for a third-party product; low value for a new product |
| D-16.51 | Non-English locales (16 catalogues), docs translation | English only, with the catalogue mechanism and the key-parity check live | Translation must be written fresh; owner decides languages and workflow (10 Q13) |
| D-16.15 | Termux APT channel, MSIX/Store | Not in the 1.0 release flow unless P24 has capacity | P5 ("later"); signing and store accounts needed |
| D-16.12 | Vendor portal provider and its commands | Not built (P3) | Product decision |
| D-16.11 | Impersonating subscription OAuth | Not built (P2) | Legal/product decision |

## 3. Decisions

Date for every row below: 2026-10-18. "Lead" = the build lead; "owner" = the product owner.
Rows marked **open — owner** need the owner's answer; each carries our recommendation so the build
can proceed on it until answered (builders treat the recommendation as provisional and keep the
change cheap).

### 3.1 Settled

| ID | Decision | Status |
|---|---|---|
| D-01 | Build method: Read → Rewrite → Compare, clean room, n-gram and banned-identifier guards in CI | ✅ Lead, 2026-10-18 |
| D-16.1 | Not wire-compatible with the reference | ✅ Lead, 2026-10-18 |
| D-16.2 | Not storage-compatible; own tables, files and keys | ✅ Lead, 2026-10-18 |
| D-16.3 | No legacy migration or compat paths (§2.3); our config stamp and store schema start at 1 | ✅ Lead, 2026-10-18 |
| D-16.4 | Scope is full parity, delivered in the phase order of `15-roadmap.md` | ✅ Lead, 2026-10-18 |
| D-16.5 | Product name "Indus Agent", CLI `indus`, `INDUS_`, `~/.indus` | ✅ Owner intake, 2026-10-18 (trademark check open, D-16.40) |
| D-16.10 – D-16.17 | P1–P8 as intended differences | ✅ Lead, 2026-10-18 |
| D-16.18 – D-16.24 | Lead defaults (security hardening, tokenizer, env names, fresh copy, follow code) | ✅ Lead, 2026-10-18 (owner may override) |
| D-16.25, D-16.27 – D-16.32, D-16.34, D-16.37 – D-16.39 | Spec-driven intended differences in §2.7 | ✅ Lead, 2026-10-18 |
| D-16.41, D-16.49, D-16.50 | Not in 1.0 (§2.8); the other §2.8 rows wait on §3.2 | ✅ Lead, 2026-10-18 (owner may pull any into 1.0) |

### 3.2 Open — owner (with our recommendation)

| ID | Question (source) | Recommendation | Status |
|---|---|---|---|
| D-16.40 | Trademark and licence review of "Indus Agent"; wording of the MIT credit in `THIRD_PARTY_NOTICES.md` | Keep the name; run a trademark search before public launch; credit the reference's copyright line verbatim in notices only | open — owner |
| D-16.42 | Which provider profiles ship bundled in 1.0 (01 Q2) | Everything reachable with an API key or a documented OAuth/device flow: aggregator (OpenRouter), Anthropic, OpenAI, Gemini, Bedrock, Vertex, Azure, DeepSeek, xAI, Mistral, Hugging Face, NVIDIA, Fireworks, DeepInfra, local servers (Ollama, LM Studio, vLLM via custom), plus named custom endpoints. Skip any whose only access path is undocumented or impersonating | open — owner |
| D-16.43 | Size of our bundled starter skill set (07 Q13) | ~15 skills covering the most common jobs (git workflow, code review, debugging, writing, research, data wrangling, scheduling, self-help for the product itself); more via the hub | open — owner |
| D-16.44 | Dashboard login for non-loopback use (09 Q8) | Username/password with argon2 hashes plus generic OIDC; no vendor portal login | open — owner |
| D-16.45 | Run a curated plugin / optional-MCP catalog? (07 Q14) | Not in 1.0; reconsider once there is a review process and hosting | open — owner |
| D-16.46 | Out-of-process plugin host in 1.0? (07 Q12) | No; post-1.0. In-process with allow-list, static scan, consent, and pre-tool hooks fail closed | open — owner |
| D-16.47 | Keep a separate native setup installer? (08 Q10) | No; in-app installer only | open — owner |
| D-16.48 | Ship a light remote-only desktop variant? (08 Q11) | No; one variant | open — owner |
| D-16.51 | Locales at 1.0 and translation workflow (10 Q13) | English only at 1.0; mechanism and parity check live; add languages with human review after launch | open — owner |
| D-16.53 | Default approval mode on a fresh install (02 Q13) | `manual` (ask) by default; smart pre-judge opt-in during setup | open — owner |
| D-16.55 | Windows scope (04 Q10, R09.19) | Tier 1: WSL2 for CLI, TUI, gateway; native Windows for the desktop app and the PowerShell installer; native-Windows terminal backend (Git Bash) best-effort in P25 | open — owner |
| D-16.56 | Versioning scheme (04 Q4) | SemVer starting at 0.x during the build, 1.0.0 at release; canary builds as `x.y.z-canary.<ts>` with channel order from the channel record (R10.26) | open — owner |
| D-16.57 | `update` scope in 1.0 (04 Q7) | Stable and canary channels, transactional update with rollback, restart of this profile's gateway; no fleet-wide restarts or one-off channels | open — owner |
| D-16.58 | Release artefact hosting (10 Q9) | GitHub Releases + GHCR for images in 1.0; object storage with channel records later | open — owner |
| D-16.59 | Trajectory format consumers (10 Q10) | Our own documented JSONL format, with think blocks applied consistently; no promise of byte-equality with the reference | open — owner |
| D-16.60 | Default tokenizer and summariser for trajectory compression (10 Q11) | A permissively licensed tokenizer pinned by revision, remote code off; summariser = the configured auxiliary model | open — owner |
| D-16.61 | REA registration (README Stage 1) | Approve, so the parity pass and the "needs REA" questions can be answered | open — owner |
| D-16.62 | Default turn (iteration) cap (01 Q1) | Finite default of 150 iterations with the grace summary call; `0` means unlimited | open — owner |
| D-16.63 | Model-visible tool names (02 Q10) | Keep plain functional names (`terminal`, `read_file`, `patch`, …) since they describe behaviour and help models; rename every branded or vendor-prefixed tool | open — owner |
| D-16.64 | Docker default image (02 Q1) | Publish our own slim sandbox image (and a browser/desktop variant for browser and computer-use tools); never default to the reference's image | open — owner |

### 3.3 Open — lead (proposed, build proceeds on the proposal)

| ID | Question (source) | Proposal | Status |
|---|---|---|---|
| D-16.26 | API server key on loopback (06 Q13) | Always require a key | proposed — lead |
| D-16.36 / D-16.54 | "every Nd at HH:MM" (07 Q1, R07.10) | Accept as daily/N-daily at that wall-clock time in the configured zone | proposed — lead |
| D-16.52 | Pinning policy (10 Q7, R10.11) | Exact pins for direct deps, bounded ranges only where a library demands it; uv `exclude-newer` 14 days at lock time; full-SHA git sources; CI check enforces exactly this text | proposed — lead |
| D-16.65 | Persona and project file names (01 Q5) | `PERSONA.md` in the profile home; `INDUS.md` or `.indus.md` in projects; also read `AGENTS.md`, `CLAUDE.md`, cursor rules | proposed — lead |
| D-16.66 | Prompt guidance blocks and model-family gating (01 Q3) | Keep the idea of per-family guidance, write our own text and our own family lists; golden prompt tests per surface | proposed — lead |
| D-16.67 | MCP serve `events_wait`: push or long-poll (09 Q11) | Long-poll in 1.0 driven by a store change counter; MCP notifications when client support is common | proposed — lead |
| D-16.68 | Dashboard pages in 1.0 (09 Q7) | All pages, since the gateway exists by P18; git/audio/local-model routes ship with the desktop in P20 | proposed — lead |
| D-16.69 | Cross-process turn lease timing (01 Q11) | Built in P17 with the daemon; before that, a simple per-session file lock prevents two in-process surfaces from running one session | proposed — lead |

> Not legal advice. Before shipping under the brand, have counsel review the trademark and licence
> points (D-16.40).
