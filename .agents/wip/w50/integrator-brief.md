# Wave 50 integrator brief

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify26. First:
`git -C <verify26> checkout -q --detach main` (main's HEAD; verify26 is clean after wave 49). Read: AGENTS.md, HANDOFF.md,
.agents/wip/w50/brief.md and README.md (one section per slice, with merge notes), each builder's task note
docs/notes/followups/w50-<x>.md (they arrive with the patches), LEAD-NOTES "Wave 49 landing notes (integrator)" as the shape
for your own notes.

Apply with `git apply --3way <ABSOLUTE path>` in order a, b, c, d, e, f (all patches are relative to `7eee12d`).
- a + b: both touch `service/picker_reads.py`, `service/daemon_slash.py`, `gateway/ws/methods.py` (docstring),
  `tests/contract/gateway/test_ws_tui_session.py`, `test_ws_tui_reads.py`, threat model B-DAEMON-5. Keep both sides
  (a: DAEMON_REFUSED keeps only `queue`; b: DAEMON_QUICK_EXEC removed). Generated files (packages/protocol/src/generated/*,
  src/indus/protocol/field_docs.json, docs/user/reference/protocol.md, and any twin f adds) are NEVER hand-merged: take
  either side, then regenerate with the repo's tools (`python -m indus.protocol.codegen`, `field_docs`, `gen_reference.py`,
  and f's twin generator) and confirm every `--check` is clean.
- Integration text: D-ids from D-16.1150 in slice order (one §2.7 row per decision, §3.1 range row, labels in each note
  carry their D-id). Close D-16.549 (attached config.set); D-16.1094 now served (quick exec + shell.exec); D-16.1122 row and
  W49-b-6 text: Windows update signature verification is now on (w50e). RS30/RS33 in release-checklist.md and
  docs/notes/P17/ws-endpoint.md: config.set is served. Record the open question W50-b-2 (attached `!command` on host vs
  sandbox) as an owner question in §3.2. Follow-ups from every report go into your LEAD-NOTES section.

Rules: ENV RULE exactly as in brief.md with S=<scratchpad>/int50 (own .venv `uv sync --locked --offline`; node_modules
already present in verify26 — keep it). No network beyond what the gate already uses (Electron zip verified by SHASUMS),
no secrets, no commits, no stash, never pkill by pattern, never write under the real ~, /root or /. Logs in
<scratchpad>/int50/logs with a summary.txt line per step.

Full gate: uv sync --locked; ruff check + format --check; pyright; lint-imports; codegen/twins/field_docs/gen_reference
--check; installers/pins; mkdocs --strict; notices; `tooling.checks --require-ref`; biome check/format; tsc in all
projects; check:ipc, gen:bridge-types --check, dump:slash --check; root vitest (CI=true); `INDUS_STRESS_SECONDS=10 pytest -m
"not perf"` (--basetemp=/tmp/i50pt); `pytest -m perf` alone; INDUS_RELEASE_LANE=1 upgrade e2e; Electron E2E under xvfb; web
Playwright. Then stage everything with `git add -A` (do not commit) and report: files, conflicts and how resolved, D-ids,
gate counts, owner items, follow-ups. Clean up /tmp dirs your gate created (literal paths only).
