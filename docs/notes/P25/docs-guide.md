# Task: User guide content (P25-16) Phase: 25 Owner: builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none for the writing (the row says:
write from our own plan and code, no reference prose). For the Compare step only, the docs folder
listing of `website/docs/` (directory and file names, to check section coverage; no page opened).
Evidence: none.

What it does (own words): the hand-written pages of the user docs site, in our voice, built from
our code, the generated reference pages, the phase notes and `16-what-we-change.md`: getting started
(install per channel, quickstart, Windows/WSL2, updating and removing), each surface (classic CLI,
TUI, desktop, dashboard, Docker), models and keys, profiles, sessions, tools and backends (P21's
four sandbox backends included), approvals and security, memory and skills, plugins and MCP, cron,
research tooling, messaging (an overview plus one page per platform or platform family) and a short
developer guide (architecture, protocol, adding a tool, a backend, a platform). Existing pages
(`acp.md`, `mcp-serve.md`, `research/evals.md`, `reference/trajectory-format.md`) stay where they are
and join the navigation.

Runnable examples: a fenced `console` block right after a `<!-- test -->` marker is a docs test.
`$ indus ...` lines are commands, the following lines their expected output (`...` matches any
lines; a line ending in `...` matches by prefix). `tests/unit/docs/test_examples.py` runs every
block in a fresh home and folder through the real entry point with the fake model
(`tests.integration.cli.fake_runner`, `<!-- test script=NAME -->` picks the model script).

Inputs → outputs: our code and plan → `docs/user/**/*.md`, `tests/unit/docs/test_examples.py`.

Edge cases / failure modes:
- an example whose output names a temp path: matched by prefix (`Set ... in ...`).
- commands other than `indus` in a tested block are refused by the harness, so a docs test can
  never run arbitrary shell.
- a marker not directly above a `console` fence is an error, not a silently skipped example.
- pages must not name flake outputs that do not exist (`tests/unit/packaging/test_nix_files.py`
  reads every page under `docs/user/`): only `default`, `minimal`, `messaging`, `tui`, `web`.
- no credential-shaped literals in examples (placeholders only).

Our design differences / decisions:
- Docker page: the Windows compose file needs a configured login provider (D-16.25, D-16.755); the
  page shows both ways (password through an attached `run`, or OIDC through `config set`).
- Release URLs in the install page use the release base `indusagi/indus-agent`
  (`RELEASE_REPOSITORY`, owner-open D-16.690); change both together when the owner decides.
- Messaging: one page per platform (WhatsApp Business and the personal bridge on one page, WeCom's
  two integrations on one, BlueBubbles and Photon under iMessage, API server / webhooks / Graph on
  one). Vendor console steps are short and named as the vendor names them.

Tests we will write:
- every tested example runs green against the fake model (11 blocks on 11 pages);
- the harness: parsing, refusals, wildcard matching.

## Decisions for the lead
- The install page documents adding an extra to a script install through
  `updates.installs.<id>.extras` + `indus update` (what the installer and the updater read today).
  If a friendlier command lands later, update that section.
- The native-Windows terminal backend (P25-24) is described as best effort in the Windows page; it
  is not built yet in this wave.

Compare checklist (filled after the rewrite; reopened only the listing of `website/docs/`):
- [x] getting started: install, quickstart, platform support (our Windows page), updating
- [x] user guide: CLI, TUI, desktop, Docker, profiles, sessions, security, configuration (generated), messaging per platform, features (tools, memory, skills, MCP, plugins, hooks, cron, delegation, code execution, browser, voice via STT, ACP, API server, dashboard)
- [x] developer guide: architecture, adding tools / platforms / backends, protocol
- Deliberate differences: no guides/tutorials section and no integrations-per-provider pages in 1.0 (providers are in the generated reference); no translations.
- Not taken: pages for features we do not ship in 1.0 (kanban, pets, skins, bot screen, credential vault, egress proxy) — P24 features get pages when their wiring lands (follow-up).

## Status

Done (2026-10-21). See the final report.

## Wave 46 (RS10, builder h, 2026-10-22)

Read: no reference file (user pages written from our code and the P24 task notes only; no
reference prose opened). Evidence: none.

What changed: user-guide pages for the P24 features wired in waves 40–42, each checked against the
code it describes (CLI docstrings and options, config schema rows, the desktop strings and RPC
calls): `user-guide/board.md` (`indus kanban`, dispatcher, worker outcomes, board toolsets),
`user-guide/bots-and-peers.md` (bot chat, `self-chat` delivery, the Bots page, `indus peer`),
`user-guide/rooms.md` (group rooms: where they run, turn policy, stop words, indeterminate turns,
disband, limits), `messaging/relay.md` (gateway side of the relay; links the connector contract),
`user-guide/themes.md` (desktop presets, accent, per-profile, VS Code import, TUI sync),
`user-guide/pets.md`, `user-guide/vault.md` (`browser_vault` sources, `indus vault`),
`user-guide/egress.md`, `user-guide/helper-programs.md` (`indus pm`), `user-guide/lsp.md`,
`user-guide/verify.md`. All in the mkdocs nav (user guide; relay under messaging).

Left out on purpose (not confirmed as reachable for a person today): `message_agent` (bot chats do
not run with the `bots` platform yet; P45 is wiring it), room creation from a UI (the desktop has
no create button; the page names `groups.create`), room approvals, plugin locale packs.

## Decisions for the lead
- W46-h-1 (D-16.1014): the pages document current behaviour only; when P45 lands (`message_agent` in bot chat,
  `indus lsp restart` against a running daemon), `bots-and-peers.md` and `lsp.md` need a paragraph
  each (today `indus lsp restart` acts on its own process, as its help says).
- W46-h-2 (D-16.1015): no `<!-- test -->` examples were added: every new command either needs a daemon, a
  terminal for hidden prompts, the network or an image service; the fake-model harness cannot run
  them. The pages' command lines were checked against the generated CLI reference instead.

Integration (waves 45 + 46 integrator, 2026-10-22): with P45 landed, `bots-and-peers.md` gained
"Bots that message other bots" (`message_agent` in a running gateway's bot chats, D-16.980) and
`lsp.md` a paragraph on `indus lsp restart` asking the gateway first (D-16.983, per profile with
D-16.1021); W46-h-1 is done.
