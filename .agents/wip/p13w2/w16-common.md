P13 wave 2 (MCP, plugins, hooks, skills hub) is built in four parallel slices from main ad24cb5
(P00–P12 done; P13 wave 1 landed in 78fdc96: P13-01..04 MCP config/naming/transports/lifecycle,
P13-13..17 plugins + hooks bus, P13-20 content scanner, P13-24 memory provider API). Read first:
docs/plan/phases/P13-mcp-plugins-hooks-hub.md (your task rows, files, traps, acceptance),
docs/plan/phases/LEAD-NOTES.md (all landing notes, especially P13 wave 1 and waves 7–10: the seams
for M1's connection API, the hooks bus, plugin manifests, content_scan, memory providers), and
HANDOFF.md "NEXT STEP" (deferred seams to wire).
Slices (each owns only its tasks' files):
- N1: P13-05..08 — MCP discovery and tool registration, calls, trust and circuit breaker,
  reload / list_changed (on M1's connection API).
- N2: P13-09..12 — MCP OAuth, sampling and elicitation, OSV preflight, `indus mcp` CLI. N1 builds
  registration/calls in parallel: stand in what you need from it.
- N3: P13-18 prompt sections, P13-19 shell hooks + `indus hooks`, P13-21..23 plugin install /
  consent / CLI (use content_scan). Also wire the deferred `pre_llm_call` hook in core/prologue.py.
- N4: P13-25..28 — skills hub and project-skill trust (P09's skills tiers reserve the project tier;
  learning/skills/resolver.py).
Cross-slice: test-only stand-ins under tests/.../_stubs_<slice>.py with FIRST LINE "# STAND-IN:
removed at integration" (never in src/) for anything another slice owns.
Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
D-ids: do not number them; list intended differences under "Decisions for the lead" in your notes.
pyproject.toml: only deps your tasks name (exact pins, uploaded before the exclude-newer date); CI
installs no optional extras, so optional imports must be pyright-ignored and tests needing an extra
must skip cleanly without it. Leave uv.lock to the lead.
Every task: note first in docs/notes/P13/<slug>.md; Read the reference paths in the row, close them,
write; Compare after; tests per the Verify column (fake MCP servers over stdio/HTTP in-process or
local, fake clock, tmp homes only — never write under the real ~ or /root, no external network);
update only your trap/ACC rows. No credential-shaped literals (OAuth tokens built at runtime).
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. On that message, stop starting new tasks, get what you
have to a passing state for the finished tasks, and report which tasks are done / partial.
