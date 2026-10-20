# Task: Backend interface and `--local` host (P19-03) Phase: 19 Owner: wave-29 builder A1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/server.py` (the
in-process agent: initialize, session new/load/resume/fork/list, list paging, modes, cancel — to see
what the in-process path did differently from the gateway path), `acp_adapter/session.py`
(`SessionManager`: create/get/fork/list/update_cwd, persistence, agent factory). Our P04
`service/session_service.py`, `tui_bridge/host.py` (how a surface builds the profile's service),
`state/rewind.branch`, `service/creation_policy.py`. Closed before writing.
Evidence: none.

What it does (own words): every ACP handler talks to one interface, `AcpBackend`, so the editor sees
the same behaviour whichever host runs the agent (D-16.29, R09.13). The interface has: `create`
(a `CreateRequest`: cwd, request id, editor policy, frozen launch policy), `resume` (attach and
return a snapshot: current id, cwd, title, model, provider, transcript rows, pending server
requests, lost turns, replay epoch/last seq), `fork`, `list_sessions`, `submit` (with an `input_id`
for idempotent retries), `cancel` (by admission), `mutate` (named operations: `workspace`,
`branch`, `rename`, `compress`, `editor_mcp`, `model`, `rewind`, `reset`), `subscribe` (events with
`seq` + `replay_epoch`, server requests with their generation, gaps), `answer` (prompt id +
generation), `receipt`, `usage`, `show_reasoning`, `close_session` (detach only) and `aclose`.

`LocalHost` implements it over an in-process `SessionService` built the way the TUI bridge builds
one (route, the profile store, built-in tools, MCP/plugins upkeep, the terminal runtime),
constructed off the event loop. Sessions are created with source `acp`, the platform toolset for
`acp`, the editor's cwd, and the same editor policy attached mode sends (ask before edits; editor
MCP specs kept in memory only). The per-session cwd is the host's: the agent factory puts the
session's cwd into each agent it builds, so a workspace move (idle only) is "store the new cwd,
rebuild the agent". Admissions: one per submit; an `input_id` seen before returns its first
admission (the editor's retry of a lost reply never starts a second turn). There is no durable
ledger here, so receipts are `terminal`/`started` from this process's memory; after a restart the
session resumes from the store with no open turn ("unknown" never happens in local mode).

Mode selection (`select_backend`): `--local` → `LocalHost`. Otherwise the attached host; when it
finds no daemon, `acp.allow_local_fallback: true` switches to `LocalHost` once (logged), and the
default `false` (D-16.83) answers `initialize` normally and fails `session/new` (and load/fork)
with "No Indus gateway daemon is running for this profile. Start it with `indus gateway start`,
or run `indus acp --local`."

Inputs → outputs: handler calls → snapshots, admissions, event callbacks.

Files: `src/indus/acp/backend.py`, `src/indus/acp/local_host.py`, `src/indus/acp/store_view.py`
(read-only store reads shared by both hosts), `tests/unit/acp/test_local_host.py`; shared:
`src/indus/foundation/config/schema.py` (`acp.allow_local_fallback`) + its test,
`src/indus/state/sessions.py` (`set_cwd`).

Edge cases / failure modes:
- Unknown session id → `not_found`.
- Workspace move while a turn runs → `session_busy`.
- `model`/`rewind`/`reset` mutations: served here since P19-12 (`model` rebinds the session's
  agent when idle, or waits for the turn boundary; `rewind` takes user turns back; `reset`
  empties the conversation; both rebuild the agent from the store). Any other unknown
  operation → `mutation_unavailable`.
- Building the service fails (no provider): the error text reaches `session/new`.
- Close: detaches the sessions this process holds; nothing is ended.

Our design differences / decisions:
- One interface, two hosts; the reference's in-process three-mode set is not built (D-16.29).
- `input_id` idempotency in local mode is in memory only (no ledger).

Tests we will write:
- the same handler suite (in `tests/contract/acp/`) against `LocalHost` and the attached host
  (parametrised fixture);
- no daemon + fallback off → `session/new` error text; fallback on → local; `--local` → local;
- input id retry → same admission, one user row; workspace move rebuilds the agent's cwd; busy
  move refused.

## Decisions for the lead
- Mutations `model`, `rewind`, `reset` were declared here and answered `mutation_unavailable`
  until P19-12; P19-12 (`docs/notes/P19/acp-commands-model.md`) now serves all three on the
  local host (corrected at the P19-08..18 integration).
- `state/sessions.set_cwd` added (shared): updates `cwd` only (git root/branch kept).

Compare checklist (filled after the rewrite; reopened `server.py`, `session.py`):
- [x] in-process create/load/resume/fork/list paths covered by the interface
- Deliberate differences: no in-process mode set; no silent fallback (D-16.83).

## Status

Done (2026-10-20), wave-29 builder A1. Tests: see the task's Verify line; all green with the gate checks. Trap/ACC ledger rows for R09.9/R09.13/R09.14/R09.16 and ACC-M1/M2/M8 left to the lead (follow-up).
