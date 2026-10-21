# Task: Board tools, review flow and `indus kanban` (P24-13) Phase: 24 Owner: wave-37 builder B1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/kanban_tools.py` (gating,
worker guard, ownership rule, every handler, the registration table and the orchestrator-only
pair), `hermes_cli/kanban.py` (module header, flag helpers, `_cmd_create`, `_cmd_list`, the list
of `_cmd_*` subcommands). Research 02 §3.28, R02.25, R04.19. Closed before writing.

What it does (own words): the agent's handle on the board. Fifteen tools: `kanban_show`,
`kanban_list`, `kanban_complete`, `kanban_block`, `kanban_schedule`, `kanban_request_review`,
`kanban_request_changes`, `kanban_heartbeat`, `kanban_comment`, `kanban_attach`,
`kanban_attach_url`, `kanban_attachments`, `kanban_create`, `kanban_unblock`, `kanban_link`. In a
worker session (started by the dispatcher) the task id defaults to the worker's own task, a worker
may change only its own task, and the run-ending tools prove the run id the dispatcher pinned, so a
stale worker cannot end a successor's run. `kanban_list` and `kanban_unblock` route the board and
belong to orchestrators only: they are absent from a worker's tool list and refuse if called
anyway. The tools run inside the agent process, so they reach the board file even when the
terminal runs in a container. People use `indus kanban`.

Inputs → outputs: tool calls → JSON results (`{"ok": true, ...}` or `{"error": ...}`); `indus kanban
board|create|list|show|move|comment|attach|assign|review|dispatch|archive` → text or `--json`.

Files: `src/indus/tools/builtin/board.py`, `src/indus/cli/commands/kanban.py`,
`src/indus/tools/toolsets.py` (shared: board toolsets), `src/indus/cli/app.py` (shared: lazy group),
`src/indus/tools/builtin/__init__.py` (shared: module list), `src/indus/acp/tool_kinds.py` (shared:
ACP kinds of the 16 new tools), `tests/contract/cli/non_tty_table.yaml` (shared: rows),
`tests/contract/cli/test_help_catalogue.py` (shared: golden order), `tests/contract/tools/
test_reachability.py` (shared: opt-in sets), `tests/unit/tools/builtin/test_board.py`,
`tests/unit/cli/commands/test_kanban.py`.

Toolsets (R02.25): tools register into `kanban-worker` (13) and `kanban` (the orchestrator pair);
`kanban` includes `kanban-worker`. Both are opt-in: "all" never pulls them in; a worker session is
given `kanban-worker`, an orchestrator enables `kanban`. Membership comes from the registry, so
`kanban_schedule` is reachable wherever the toolset is on.

Edge cases / failure modes: complete without summary or result → refused, nothing changed;
attach over 25 MiB → refused; `attach_url` only http(s) through the URL policy and the safe
transport, size checked while streaming; comment author is the session's identity, never an
argument; request_review to a profile that does not exist → refused; CLI without a terminal never
prompts: missing arguments are usage errors (2), bare `indus kanban` lists (0).

Our design differences / decisions:
- worker identity comes from the scope's env view (`INDUS_BOARD_TASK`, `INDUS_BOARD_RUN`,
  `INDUS_BOARD_CLAIM`, `INDUS_BOARD_SLUG`), never `os.environ`;
- opt-in toolsets are a new `Toolset.opt_in` flag (excluded from "all");
- `kanban_create` subscribes the calling chat (the delivery origin the gateway passes) to the new
  task so its outcome is posted back;
- CLI verbs per the row; `move` covers manual status changes (ready/todo via unblock, blocked,
  done, archived); `review ID --approve | --changes REASON`; `board [SLUG]` lists boards or
  switches the current one (stored in `<root>/boards/current`).
- left out: goal-mode judge gates, phantom card checks, swarm, diagnostics, notify-* verbs (chat
  subscriptions are made by the tool and the watcher).

Tests we will write: worker session tool list lacks list/unblock; `kanban_schedule` present when
the toolset is on and absent from "all"; complete without a handoff refused; worker cannot touch
another task; stale run id refused; attach cap; attach_url refuses a private address; CLI non-TTY
table rows; create/list/show/move/review round trip through the CLI; `--json` output parses.

## Decisions for the lead
- `Toolset.opt_in` is new in `src/indus/tools/toolsets.py`; selection "all" skips opt-in sets. The
  P04-11 reachability contract now counts a tool reachable from "all" or from a named opt-in set.
- `kanban` and `project` sit after `webhook` in the top-level command order (golden list updated).
- Non-TTY table rows added for `kanban`, `project` and their subcommands.
- The gateway should pass `delivery_origin` (as the cron tool gets it) so `kanban_create` can
  subscribe the chat; until then tasks created from chats are not auto-subscribed.

Compare checklist (reopened the files above):
- [x] all 15 tools registered, list/unblock orchestrator-only (`tests/unit/tools/builtin/test_board.py::test_worker_session_lacks_list_and_unblock`)
- [x] `kanban_schedule` reachable through the registry (`::test_schedule_present_when_the_toolset_is_on`)
- [x] worker ownership and run-id proof (`::test_worker_cannot_touch_another_task`, `::test_lifecycle_tools_need_the_pinned_run`)
- [x] complete needs a handoff (`::test_complete_without_a_handoff_is_refused`)
- [x] attach cap, attach_url policy (`::test_attach_cap_and_url_policy`)
- [x] comment author not from args (`::test_comment_author_is_the_session_identity`)
- [x] CLI verbs and non-TTY (`tests/unit/cli/commands/test_kanban.py`)
- Deliberate differences: opt-in toolsets instead of a check function reading the env; CLI verb set
  per the row.
- Not taken: goal-mode gates, created-cards verification, worker comment polling into the live
  session, dashboard plugin.

## Status

Done (2026-10-21).
