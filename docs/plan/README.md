# Indus Agent — clean-room rebuild plan

Indus Agent is a self-improving AI agent written from scratch. It re-implements the behaviour of a
public MIT-licensed reference agent without copying its code, text or assets.

## Intake decisions (Stage 0, 2026-10-18)

| Decision | Value |
|---|---|
| Product name | Indus Agent; CLI `indus`; env prefix `INDUS_`; home dir `~/.indus` |
| Reference | `github.com/nousresearch/hermes-agent` |
| Pin | `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (2026-10-18) |
| Reference checkout | `/root/reference/hermes-agent` (outside this repo, read-only) |
| Licence | MIT, Copyright (c) 2025 the reference's authors. Credit planned in `THIRD_PARTY_NOTICES.md`. Not legal advice; licence and trademark questions go to the owner. |
| Scope | Full parity: agent core, tools and 7 terminal backends, sessions/state, CLI, TUI, messaging gateway, learning loop (memory, skills, plugins, cron, MCP), desktop app, web UI, ACP and MCP server, research tooling, packaging |
| Stack | Python, as in the reference (TUI and desktop keep the reference's runtime choice unless the what-we-change doc says otherwise) |
| Repo | `indus-agent` (new, private), default branch `main` |
| Agents | Two agents share the build through the GitHub handoff protocol (`HANDOFF.md`, `.agents/turn.json`) |

Banned identifiers in product code (seed list, extended from each spec's section 2):
`hermes`, `Hermes`, `HERMES_`, `nous`, `Nous`, `NousResearch`, `nousresearch`, `.hermes`, `hermes-agent`.

## Reference size at the pin

About 20k tracked files. Product code outside tests is roughly 2.2M lines: Python about 1.0M
(the CLI package about 289k, the agent core about 143k, tools about 127k, the gateway about 119k),
the desktop app about 837k (TypeScript/Electron), the TUI about 117k plus its bridge about 46k, and
the web UI about 62k. Tests are about 1.46M lines.

## Research (Stage 1)

One behavioural spec per area in `research/`:

| # | Area |
|---|---|
| 01 | Agent core loop and model providers |
| 02 | Tools and terminal backends |
| 03 | State and persistence |
| 04 | CLI, configuration, auth and lifecycle |
| 05 | Terminal UI |
| 06 | Messaging gateway |
| 07 | Learning loop, skills, plugins, cron, MCP client |
| 08 | Desktop app and installer |
| 09 | Web UI, ACP adapter, MCP server, platform layer |
| 10 | Research tooling, packaging, CI, docs, i18n |

REA status: owner approved 2026-10-18; rea-agents 6.7.0 registered for Claude Code (doctor healthy). MCP tools load in the next session; the CLI fallback works now.
The specs were first written from source reading only; claims that need a shipped-build or runtime
check are listed as open questions marked "needs REA".

## Product decisions after research (2026-10-18)

Each becomes an entry in `what-we-change.md` when that doc is written (Stage 3).

| # | Decision | Source |
|---|---|---|
| P1 | Build the agent in-process first: classic CLI and TUI run the agent locally (the TUI talks to a child process over stdin/stdout). The shared per-profile gateway daemon comes in a later phase and becomes the parity path for every surface. | R04.1, R05.1, 09 Q1 |
| P2 | No subscription OAuth route that impersonates another vendor's client. API keys and officially documented OAuth/device flows only. | R01.12 **Reversed for OpenAI only by the owner, 2026-10-25 (D-16.1282):** ChatGPT sign-in through the Codex CLI's OAuth client, as the reference does; the owner accepts the provider-terms risk. Everything else in P2 stands (no Anthropic subscription route, no identity lines, no borrowed CLI logins). |
| P3 | No first-party hosted portal provider; the free-tier, top-up and subscription commands are dropped. | 01 Q2, 04 Q8 |
| P4 | Secrets default to the OS keychain, falling back to owner-only 0600 files. | 08 Q8 |
| P5 | Distribution as in the reference: installer script, Docker, Nix (later Termux and MSIX). No PyPI. | 10 Q1 |
| P6 | Cron keeps the reference grammar: a bare duration repeats; `in <dur>` runs once. | 07 Q11 |
| P7 | Terminal backends: local, Docker and SSH in the first tools phase; Singularity, Modal, Daytona and Vercel Sandbox in a later phase behind the same contract. The vendor-hosted managed Modal gateway is dropped. | 02 Q11-12 |
| P8 | Gateway v1 runs one profile per process; multiplexed profiles and hosted rooms come in a later gateway phase. | 06 Q8 |

Lead defaults (the owner may override):
- Security hardening: every desktop IPC handler checks its sender frame, the renderer has a strict CSP, Electron fuses are set, and untrusted content can never open URLs (an intended difference).
- The tokenizer in trajectory compression never runs remote code by default.
- Platform credential env vars keep their vendor-standard names (`TELEGRAM_BOT_TOKEN`); product env vars use `INDUS_`.
- All prompt text, UI copy, playful strings and bundled skills are written fresh; none is carried over.
- The specs follow the code when the reference's docs and code disagree.

## Owner decisions, round 3 (2026-10-18)

- Indus Agent ships under the **MIT** licence (LICENSE lands with P00).
- Windows: WSL2 for CLI, TUI and gateway at 1.0; desktop app and PowerShell installer native (D-16.55).
- English only at 1.0, with about 15 freshly written starter skills (D-16.43, D-16.51).
- Default approval mode on a fresh install is manual; smart is opt-in (D-16.53).
- Secrets: the OS keychain default (P4) covers CLI secrets too, not only desktop (inventory N31).
- The other open rows in `16-what-we-change.md` §3.2 are accepted on the planners' recommendations
  by the lead; the owner may override any of them. REA registration (D-16.61) was approved the same day.
