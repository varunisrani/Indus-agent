P15 waves 2–3 (gateway delivery, chat surfaces, daemon) are built in four parallel slices from main d0f29d0.
P00–P14 are done; P15-01..18 landed (fake platform + contract suite, gateway config, inbound
events, send/retry, adapter base, registry/token locks, session keys + routes, authz, pairing (store in
state/), bot-loop/concurrency/tiers, admission pipeline, busy guards, chat commands, chunker,
formatters, stream consumer, outbound sender, media policy, PlatformReplies). P14 cron (store, ticker,
runner, delivery targets + local deliverer in src/indus/cron/) and P13 (PluginUpkeep, McpUpkeep) are
landed. Read first: docs/plan/phases/P15-gateway-core-demo3.md in full (your rows, files, traps,
acceptance, "Done when"), LEAD-NOTES.md — especially "P15 wave 1 G1–G4 landing notes (lead)" (open
seams you now own: daemon wiring, send_with_retry in OutboundSender via the ledger, R06.5 active-turn
marker after the ledger write, identity_env vs child_env, trust_env into the proxy, token-lock
liveness) and "P14 landing notes" (target grammar, deliverer registry, ticker, D-16.230 cron + plugins),
"Gate change" — and HANDOFF.md.
Layer rule (D-16.246): cli and gateway are siblings — shared stores/services the CLI needs go in
state/ or service/; `indus gateway …` and `indus send` register by "module:attr" like tui-bridge.
Slices (each owns only its tasks' files):
- W1: P15-19 delivery ledger (state table next to gateway_routes; move the active-turn marker clear
  behind the ledger write, R06.5; OutboundSender uses send_with_retry and records undelivered pieces),
  P15-20 platform deliverers, router, channel directory, mirror, `indus send` (P14 owns the target
  grammar and DeliveryTarget — register platform deliverers into P14's registry).
- W2: P15-21 cron ticker hosted in the daemon (decide and record whether cron sessions get a
  non-interactive PluginUpkeep — D-16.230), P15-22 approvals and clarify over chat (G4's on_request= and
  interceptors= seams; cards; redaction), P15-23 intake: voice, images, documents, albums (G4's enrich=
  seam; P10 STT library; size and type limits; caches under the profile home).
- W3: P15-24 daemon process: `indus gateway run`, locks, liveness, runtime status (builds
  AdmissionPipeline, PlatformReplies, PluginUpkeep + load_plugin_entries, McpUpkeep, cap, bot guard,
  key options; replaces token_locks' private start-time helper), P15-25 drain, restart, shutdown,
  watchdogs, P15-26 boot recovery.
- W4: P15-27 control socket (POSIX Unix socket, owner-only, never TCP), P15-28 service install and
  `indus gateway` commands (systemd user units; no root), P15-29 reconnect watcher, `/platform`,
  gateway hooks (P13 hooks bus; post_gateway_admission).
Cross-slice: test-only stand-ins under tests/.../_stubs_<slice>.py with FIRST LINE "# STAND-IN:
removed at integration" (never in src/). W3 defines the daemon object other slices plug into: write its
API (what it builds, start/stop hooks, where W1/W2/W4 register) in its task notes first.
Tests drive everything through the fake platform (G1) and the fake model; daemons run in-process or as
a subprocess in a tmp home; no real network, no real platform tokens (build fakes at runtime), never
write under the real ~ or /root, no stray processes or sockets after a test.
Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
D-ids: do not number them; list intended differences under "Decisions for the lead" in your notes.
pyproject.toml: only deps your tasks name (exact pins, before the exclude-newer date); CI installs no
optional extras. Leave uv.lock to the lead.
Every task: note first in docs/notes/P15/<slug>.md; Read the reference paths in the row, close them,
write; Compare after; tests per the Verify column; update only your trap/ACC rows. No credential-shaped
literals.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. On that message, stop starting new tasks, get what you
have to a passing state for the finished tasks, and report which tasks are done / partial.
