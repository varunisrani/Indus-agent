# Task: Management pages, part 2 — agents, command center, learning map, artifacts, session import (P24-28) Phase: 24 Owner: wave-39 builder U2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/agents/index.tsx`
(panel rows: workers, active count, tools, files), `apps/desktop/src/app/command-center/index.tsx`
(sections, log viewer), `apps/desktop/src/app/starmap/` (file list; `share-code.ts` and the
constants for geometry behaviour only), `apps/desktop/src/app/artifacts/index.tsx` (filters,
indexing recent sessions, remote download), `apps/desktop/src/app/session-import/index.tsx` (source
filter, preview, import). Research 08 §3.19. Our `web/routes/{sessions,logs,analytics,system,files}.py`,
P17-21 maintenance gate (`service/maintenance_gate.py`). Closed before writing.
Evidence: none.

What it does (own words):

Agents overlay (`/agents`): a live view of delegation in the session on screen. Each
`delegate_task` call is a batch; each task in it is a worker with its goal, status (queued,
running, done, failed), elapsed time, tool count and the files it read or wrote. Active counts per
batch and in total. Our protocol has no subagent event yet: the view is fed by a reducer over the
session's events — `tool.started` of `delegate_task` (its `tasks[]` or single `goal`), its
`tool.progress` lines (a line naming a worker index updates that worker) and `tool.completed` (all
workers of the batch settle). The reducer also accepts a future `subagent.*` event shape
(`{subagent_id, parent_call_id, status, tool?, file?}`) so the view gets richer when the daemon
emits one.

Command center (`/command-center`): sections Sessions (counts and recent sessions from
`/api/sessions/stats` + `/api/sessions`, delete empty sessions), System (`/api/system/stats`,
`/api/status` health), Usage (`/api/analytics/usage?days=` 7/30/90), Maintenance (doctor, backup,
prune checkpoints, prune sessions — each starts a background action through the P17-21 gate; a 409
`maintenance_busy` answer shows who holds the gate), and a log viewer over the four log files
(agent, errors, gateway, gateway stdio) with a level filter and a text filter. The session import
panel sits in Maintenance.

Learning map (`/learning-map`): a force-directed graph of skills and memories: nodes from
`/api/skills` (skills) and a learning-graph read (`GET /api/learning/graph`, owed) when present;
edges link a memory to the skill it was learned with and skills of one category. Playback replays the
graph over time (a slider and play/pause over node timestamps); filters all / used (use count > 0)
/ learned (created by the agent). A node can be edited (skill: enable/disable) or deleted (memory).
Share exports the layout only: node kinds, quantised positions and the edge list as indices — never
a label, category, description or memory text; importing a share code shows the same shape with
"unknown" nodes. The layout is our own deterministic simulation (seeded, fixed iteration count) so
the same graph gives the same picture.

Artifacts (`/artifacts`): a gallery of images, files and links found in recent sessions' messages
(tool results and assistant text: image paths/URLs, written file paths from `write_file`/`patch`
tool calls, http(s) links), filter all/images/files/links and a search; each item jumps back to its
session. A remote connection's file is downloaded through main (`artifacts.download`): main mints
the download ticket on the connection with the item's originating profile and session, fetches the
bytes and saves them into the user's Downloads folder; the renderer never holds the ticket.

Session import: preview and import sessions written by two other coding-agent CLIs — Claude Code
(JSON lines under `~/.claude/projects/<project>/<session>.jsonl`, one record per line with `type`
`user`/`assistant`, a `message` with `role` and `content` that is a string or a list of blocks
`text`/`tool_use`/`tool_result`/`thinking`, `timestamp`, `cwd`, `sessionId`) and Codex CLI (JSON
lines under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`: a `session_meta` record with `id`, `cwd`,
`timestamp`, then `response_item` records whose payload is a `message` with `role` and `content`
blocks `input_text`/`output_text`, or `function_call` / `function_call_output`). Our mapping: user
text → `user`, assistant text → `assistant`, tool use/call → an assistant message with a
`tool_calls` entry, tool result/output → a `tool` message with the matching call id, thinking
dropped; title from the first user line (80 chars); source `import-claude-code` / `import-codex`.
Preview lists the mapped messages; import writes one session per file, refusing a file already
imported (its digest is recorded in the session's meta), and an unknown format is refused.

Inputs → outputs: Python `list_sources(home_dir)`, `preview(path, kind)`, `import_session(store,
path, kind)`; REST `GET /api/session-import/candidates?source=`, `POST /api/session-import/preview
{source, path}`, `POST /api/session-import/import {source, path}` (new router); renderer pages
above; IPC `indus:artifacts:download {path, profile, sessionId}` → `{savedTo}`.

Files: `apps/desktop/src/app/agents/{AgentsOverlay.tsx,delegation.ts}`,
`apps/desktop/src/app/command-center/CommandCenter.tsx` + test, `apps/desktop/src/app/learning-map/{LearningMap.tsx,layout.ts}`
+ test, `apps/desktop/src/app/artifacts/{ArtifactsPage.tsx,artifacts.ts}` + test,
`apps/desktop/electron/artifacts/download.ts` + test, `src/indus/service/session_import.py` (the
mapping), `src/indus/cli/importers/{__init__,sessions}.py` (the CLI-side facade),
`src/indus/web/routes/session_import.py`, `tests/unit/cli/importers/test_sessions.py`,
`tests/contract/web/test_session_import_routes.py`; shared: `web/routes/__init__.py` (one router
line), `apps/web/src/lib/api.ts` (`/api/session-import` joins the profile-scoped families),
`apps/web/src/test/routes.generated.ts` (regenerated), IPC table.

Edge cases / failure modes:
- a JSON line that does not parse → skipped and counted in the preview's `skipped`; a file with no
  recognised record → `unknown format`, refused.
- a path outside the two CLIs' session roots → refused (the route takes only listed candidates).
- an already imported file → refused with the existing session id.
- huge files → preview reads at most 2,000 records; import caps at 20,000 messages.
- learning map with zero nodes → empty state; share code from another version → refused.
- artifacts: a session whose messages fail to load → skipped with a count.
- remote download: 401/403 → "sign in again"; the path must be one an artifact named.

Our design differences / decisions:
- Delegation view derived from tool events (no subagent event in our protocol yet).
- Share code carries layout only (the reference's code carries labels).
- Session import is a REST route of the serve (the reference uses its own RPC family).

Tests we will write:
- `layout.test.ts`: layout share string contains no node text; same graph → same layout; share
  round trip keeps kinds and edges; playback window filters by time.
- `ArtifactsPage.test.tsx`: extraction kinds; remote artifact download carries the originating
  profile and session.
- `CommandCenter.test.tsx`: log level filter sends `level`; maintenance 409 shows the holder.
- `test_sessions.py`: preview lists mapped messages for both formats; unknown format refused;
  import writes one session, a second import refused.

## Decisions for the lead
- The mapping lives in `indus.service.session_import`, not in `cli/importers/sessions.py`: `cli` and
  `web` are sibling layers (import-linter), so the REST route could not import the CLI module.
  `cli/importers/sessions.py` re-exports it and adds the terminal rendering (a future
  `indus sessions import` verb). `cli/importers/__init__.py` is byte-identical to X1's (w37), so the
  union is trivial.
- The route reads the home folder from the scope's env view (`HOME` / `USERPROFILE`): route modules
  may not read the process env or `Path.home()` (test_scope rule).
- No `subagent.*` event exists on our protocol; the delegation view reads `delegate_task` tool
  events and already accepts a `subagent.update` shape (follow-up: a daemon event per child turn).
- Run history: the cron REST view carries only the last run; per-run history is follow-up (the P14
  attempt ledger has it).
- The four log files are the profile's agent / errors / gateway logs (REST) plus the desktop's own
  log (main); `gateway-stdio.log` is not served by REST.
- New REST router `session_import` (prefix `/api/session-import/`); its three routes are in its
  `ROUTE_LIST`; the generated SPA route list regenerated.
- `GET /api/learning/graph` (memories with timestamps and learned-with links) is owed by the
  learning backend; without it the map shows skills only, all at time zero.

Compare checklist (filled after the rewrite; reopened the five reference areas in `Read:`):
- [x] agents: workers, active counts, tools and files per worker (test: `CommandCenter.test.tsx::the delegation reducer counts workers, tools and files`)
- [x] command center: four sections + log viewer with level filter (test: `CommandCenter.test.tsx::the log viewer filters by level`)
- [x] learning map filters all/used/learned and playback (test: `layout.test.ts::filters and playback pick the right nodes`)
- [x] share without content (test: `layout.test.ts::the layout share string contains no node text`)
- [x] artifacts kinds and jump back (test: `ArtifactsPage.test.tsx::finds images, files and links and jumps back to the session`)
- [x] remote download with the originating profile (test: `ArtifactsPage.test.tsx::a remote artifact download carries the originating profile`)
- [x] session import sources, preview, import (test: `test_sessions.py::test_preview_lists_mapped_messages_for_claude_code`)
- Deliberate differences: layout-only share; tool-event-derived delegation view; REST import route.
- Not taken: star-map 3D tilt rendering and colour grading (our map is flat SVG), command-center
  session pinning.

## Status

Done (2026-10-21) except the learning-graph read (owed backend). Compare (reference reopened:
agents, command-center, starmap share-code/constants, artifacts, session-import): sections, filters
and the 30-session index match; our share code drops labels by design; no gaps fixed beyond the
notes above. Shared files touched: `src/app/{routes,pages,App}.tsx|ts`, `src/main.tsx`,
`src/i18n/en.ts`, IPC table (`indus:artifacts:download`), `electron/main.ts` (install step).

## Wiring (wave 42, WC, D-16.799)

Read: none (wiring).

The learning-graph read exists: `src/indus/web/routes/learning.py` — `GET /api/learning/graph`
(the `indus journey` nodes in the map's shape: skill ids `skill:<name>`, memory ids the journey's
`memory:<store>:<n>:<sha8>`, `createdBy` agent/user/null, `enabled` from `skills.disabled`) and
`DELETE /api/learning/nodes/{id}` (journey delete: a memory entry removed, a skill archived; stale
or unknown 404, pinned 409). Router registered, route list regenerated
(`apps/web/src/test/routes.generated.ts`), `/api/learning` added to the SPA's profile-scoped
families. The desktop map now gets `full: true` from the server.
Tests: `tests/contract/web/test_learning_routes.py`.

Decision for the lead: the learning layer records no "learned with" link, so the edges are skills
of one category and entries of one memory store (a star per group); a real link is a learning
backend follow-up.

Status (wave 42): the learning-graph read is done.
