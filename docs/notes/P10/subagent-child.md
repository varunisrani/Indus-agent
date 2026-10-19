# Task: child agent builder (P10-02) Phase: 10 Owner: P10 slice G builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/delegate_tool.py`
(`_build_child_agent`), `tools/delegate_tool_toolsets.py` (skim), `agent/delegation_context.py`
(all). Closed before writing.
Evidence: none.

What it does (own words): builds one child `Agent` per task. The child gets its own session row
(source `subagent`, parent id, the `_delegate_from` marker so it is a hidden "delegate" child that
goes when the parent is deleted), its own terminal task id (its session id) on the parent's
environment registry, and a tool list = (`delegation.child_toolsets`, default terminal, file, web)
∩ the parent's enabled tools, minus `clarify`, `memory`, `cronjob_manage`, every send tool and —
at depth ≥ `max_depth` — `delegate_task`. MCP toolsets of the parent come along when
`delegation.inherit_mcp_toolsets` is on. Same route as the parent unless `delegation.model` pins a
model; credentials through the same `Scope` and the parent's credential pools; iteration cap from
P10-01. Approvals reach the parent's surface through a broker wrapper that labels each request
"subagent <id>"; sudo password prompts are refused (headless). Background terminal results in a
child say "nothing will tell you when it ends: wait, kill or hand off" instead of offering notify.
The first user message is our own short brief: goal, context, the output contract when a schema
is given, and image references.

Inputs → outputs: parent agent + normalised task → `ChildAgent` (agent, ids, run/steer/stop).

Files: `src/indus/core/subagents/__init__.py`, `src/indus/core/subagents/child.py`,
`tests/unit/core/subagents/test_child.py`; shared: `src/indus/core/agent.py` (`only_tools`,
`delegation_depth` options; delegate inline wiring).

Edge cases / failure modes:
- Parent without `web` → child without `web` (never gains a tool the parent lacks).
- Blocked tools never reach the child's tool array, whatever the config lists.
- Child build failure → that task fails, the others run.

Our design differences / decisions:
- Images are listed in the brief as references (paths/URLs) for `vision_analyze`; native pixel
  attachment waits for P10-15's embed budget.
- No review fork exists yet (P09-14 not landed here): nothing to skip.
- The child never writes usage rows of its own: each answered call is rolled up into the parent's
  ledger with task label `subagent` (one count, no double totals).

Tests we will write:
- toolset intersection table; blocked tools absent; child session hidden from lists and deleted
  with the parent; approval label; child terminal background note.

## Decisions for the lead
- Docker container aliasing for persistent parent environments is left to the environment
  registry (children share the parent's registry; the key is the child's session id).

Compare checklist (reopened `tools/delegate_tool.py` `_build_child_agent`, `tools/delegate_tool_toolsets.py`):
- [x] blocked set delegate_task (at max depth), clarify, memory, send tools, cronjob_manage (test: `tests/unit/core/subagents/test_child.py::test_blocked_tools_never_reach_a_child_whatever_the_config`)
- [x] children never gain a tool the parent lacks (test: `::test_child_toolset_is_the_intersection`)
- [x] MCP toolsets kept when inherited (test: `::test_mcp_toolsets_follow_the_parent_when_inherited`)
- [x] hidden delegate session, deleted with the parent (test: `::test_child_session_is_hidden_and_deleted_with_the_parent`)
- [x] labelled approvals, headless sudo (test: `::test_child_approvals_reach_the_parent_with_a_label`)
- [x] no notify in child terminal results, hand-off note (test: `::test_child_background_terminal_result_has_no_notify_and_the_subagent_note`)
- Deliberate differences: the reference passes the brief as an ephemeral system prompt; we keep
  the parent's prompt builder with a short subagent system message and send the task as the first
  user message. The reference's 1h-cache downgrade and child compression cap are not taken.
- Not taken: per-child live transcript files, progress relay events (`subagent.*` events are not
  in the protocol yet — see below).

## Decisions for the lead (added)
- `AgentOptions` gained `only_tools` and `delegation_depth`; `Agent._open_session` wires the
  delegate inline executor like `todo_list` (lazy import of `core.subagents`).
- Usage roll-up: the child's ledger writes no rows; every child call is recorded on the parent
  ledger under task `subagent`.
- No `subagent.*` protocol events yet (P10-05 lists `protocol/events.py`); the lead may add them
  with the TUI `/agents` work — the background registry already has what `list` needs.

## Integration (lead, P10 slices G+H on P09)
- Gap found: a child reused `parent.options.builder`, which on the CLI is P09's learning stack, so
  a subagent's prompt carried the parent's frozen memory block, skills index and `-s` preloads.
  New `AgentOptions.subagent_builder` (the builder under the session-level wrappers);
  `cli/runtime.with_learning` sets it, `build_child` uses it (falls back to `builder`).
- No review fork for children: children are built in core and never pass through
  `agent_factory`, so nothing attaches a review, a `before_turn` hook or a note source to them;
  `SessionLearning.attach_review` also forces `no_review` when `delegation_depth > 0`. The curator
  runs only from the CLI start check and the service's maintenance timer, which children never
  reach (they are not service sessions).
- Children's `execute_code` kernels: `SessionRuntime.code_kernel_owner` (see the teardown note).
- Tests: `tests/integration/core/test_subagent_host.py`.

## Status

Done (2026-10-19). Shared: `core/agent.py`.
