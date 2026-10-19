P15 wave 1 (gateway foundations) is built in two parallel slices from main 8f99060 (P00–P12 done, P13
MCP landed; P13 N3+N4 (plugins, hooks, hub) and P14 (cron) are being integrated in parallel in other
worktrees — do not touch src/indus/plugins/, src/indus/cron/, learning/skills/hub or cli/runtime.py).
Read first: docs/plan/phases/P15-gateway-core-demo3.md in full (rows P15-01..10 are yours; read the
rest for context: the runner, sender, delivery, daemon and adapters come in later waves),
docs/plan/research/06-* (gateway research), docs/plan/phases/LEAD-NOTES.md (session service,
approvals, redaction, P10 automation inject/notify, P13 MCP host, gate change) and HANDOFF.md.
Decision P1: in-process first, gateway daemon later in this phase; P8: one profile per gateway first.
Slices (each owns only its tasks' files):
- G1: P15-01 fake platform harness and the shared adapter contract suite, P15-02 gateway config
  section / platform config / enablement, P15-03 inbound event model and normalisation, P15-04 send
  result / error classifier / retry policy, P15-05 adapter base contract and capability flags.
  G1 defines the shared models: write their API section in the task notes first.
- G2: P15-06 platform registry / scoped token locks / proxy, P15-07 session key builder and routing
  index (single source of truth, R06.7), P15-08 authorization chain / unknown-sender behaviour /
  mention gating, P15-09 pairing store and `indus pairing`, P15-10 bot-loop guard / concurrency cap /
  slash admin tiers.
Cross-slice: test-only stand-ins under tests/.../_stubs_<slice>.py with FIRST LINE "# STAND-IN:
removed at integration" (never in src/) for anything the other slice owns (G2 stands in G1's event,
source and adapter models until integration).
Security: secrets and tokens never in logs or events (P05 redactor); pairing codes via `secrets`;
the authorization chain fails closed.
Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
D-ids: do not number them; list intended differences under "Decisions for the lead" in your notes.
pyproject.toml: only deps your tasks name (exact pins, before the exclude-newer date); platform SDKs
belong to later adapter tasks, not this wave. CI installs no optional extras. Leave uv.lock to the lead.
Every task: note first in docs/notes/P15/<slug>.md; Read the reference paths in the row, close them,
write; Compare after; tests per the Verify column (the fake platform, fake clock, tmp homes only —
never write under the real ~ or /root, no external network); update only your trap/ACC rows. No
credential-shaped literals (bot tokens built at runtime).
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. On that message, stop starting new tasks, get what you
have to a passing state for the finished tasks, and report which tasks are done / partial.
