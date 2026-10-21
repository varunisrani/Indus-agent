# Task: ACP on the daemon — `no_mcp`, the model column, a real-daemon reattach e2e (P19 follow-ups, ACC-M8-2) Phase: 19 Owner: wave-33 builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — product follow-ups of our own
P19 landing (LEAD-NOTES "P19-08..18 landing notes", follow-ups list; D-16.635, D-16.638..642). Our
side: `acp/toolset.py` (`no_mcp`, `session_selection`), `acp/local_host.py` (`_factory`, `_switch`),
`service/session_mutations.py` (`shape`, `_switch`), `service/editor_mcp.py` (`acp_selection`),
`service/session_service.py` (`_attach`, `reload`), `service/mcp_host.py` (`selection`),
`state/usage.py` (`set_route`), `tests/e2e/acp/test_acp_daemon.py`, `tests/fakes/daemon.py`.
Evidence: none.

What it does (own words):
1. **`no_mcp` on the daemon.** `platform_toolsets.acp` may carry the marker `no_mcp`: the editor's
   sessions then get none of the profile's configured MCP servers (the editor's own servers stay).
   The local host already honours it; on the daemon an ACP session got the umbrella `mcp` toolset
   anyway, because the service added it after the agent shaper had run. Now the daemon's shaper
   decides the MCP part for ACP sessions: with `no_mcp` the umbrella and every configured server's
   `mcp-<name>` toolset are left out (the editor's `mcp-editor-<name>` kept), without it the umbrella
   is added as before. The service runs the MCP start and its umbrella step before the shaper, so the
   shaper sees the final list; a reload (which reuses the old selection) passes the shaper again.
2. **The model switch reaches the session row.** A model picked from the editor (`session.mutate
   model` on the daemon, the local host's `model` mutation) changes the running agent's route; the
   row's `model`, `billing_provider` and `billing_base_url` columns kept the old route, so a later
   resume (another process, a daemon restart) came back on the old model. Both hosts now write the
   new route with `state.usage.set_route` once the switch is applied (also a deferred pick applied at
   the turn boundary). A row not written yet (no reply so far) is left alone: the daemon's session
   fields carry the model into the first write. And a stored session now comes back on the model
   its row names when it differs from the profile's route (`stored_pick`): on the daemon an ACP
   session's shaper resolves it and `before_turn` binds it before the first turn (the mechanism
   that re-binds a pick a rebuild lost); the local host binds it right after building the agent.
   Without this the attached model picker (which shows the row's model, D-16.637) and the running
   agent would disagree after a restart. A stored model that no longer resolves keeps the profile
   route (logged at INFO).
3. **Reattach e2e (ACC-M8-2).** A real `indus gateway run` and a first `indus acp` (attached) create
   a session, prompt it and switch its model; that editor process exits; a second `indus acp`
   process loads the same session: the history is replayed, the next prompt reaches the fake model
   under the switched model, and the session row names it.

Inputs → outputs: config `platform_toolsets.acp: [..., no_mcp]` → the agent's tool names hold no
configured MCP tool; `session/set_config_option model` → row columns updated; second editor
`session/load` → `agent_message_chunk` replay, then a prompt on the switched model.

Files: shared `src/indus/service/session_service.py` (order in `_attach`),
`src/indus/service/session_mutations.py` (`shape`, `_switch`, `stored_pick`),
`src/indus/service/editor_mcp.py` (`acp_no_mcp`, `without_config_servers`),
`src/indus/acp/local_host.py` (`_switch` writes the row, `_factory` restores it); tests
`tests/unit/service/test_acp_shape.py`, `tests/contract/acp/test_model_row.py`,
`tests/e2e/acp/test_acp_daemon.py` (+ the reattach test).

Edge cases / failure modes:
- `no_mcp` alone in the saved list: the selection is the `indus-acp` bundle without MCP;
- a non-ACP session on the daemon: its selection is untouched (the umbrella as before);
- an MCP host that is not running (no servers): nothing to add or remove;
- the row write fails (store locked or gone): logged at WARNING, the switch still applies (the
  running agent is the truth for this process);
- a row whose route was replaced by its first answering route (a fallback on the very first call,
  `state.usage`) also comes back on that route — the row is what the session runs;
- a `confirm` answer (the window guard) or a refused pick writes nothing;
- reattach: the first editor's daemon-side session stays hosted after the editor exits (the daemon
  keeps it); the second load must not create a new session.

Our design differences / decisions:
- The row is written by the host that applied the switch, not by the usage ledger (the ledger only
  replaces the route on a session's first accounted call).
- Moving the MCP step before the shaper in `SessionService._attach` is the smallest change that lets
  one place decide the ACP selection; the shaper adds the umbrella itself for ACP sessions it
  filled from `acp_selection` (that list never had it).

Tests we will write:
- unit: the shaper with `no_mcp` drops the umbrella and configured server toolsets and keeps editor
  ones; without it the umbrella is there; a non-ACP session unchanged;
- unit/contract: a daemon `model` switch writes the row; the local host's switch writes the row;
- e2e: the reattach test against a real daemon.

## Decisions for the lead
- The configured-server filter uses the toolset names (`mcp-<name>`, not `mcp-editor-…`), the same
  rule as the local host's `session_selection`.
- Restoring the row's model on resume goes one step past "write the column" (asked): without it the
  written column would make the attached picker show a model the resumed agent does not run.
  Only ACP sessions on the daemon and `--local` sessions do it; CLI/TUI resume is unchanged.

Compare checklist: none (no reference read; our own follow-ups).
- [x] `no_mcp` daemon (test: `tests/unit/service/test_acp_shape.py`)
- [x] row model on both hosts (tests: `test_acp_shape.py::test_a_daemon_model_switch_writes_the_row_and_survives_a_restart`, `test_acp_shape.py::test_a_refused_or_guarded_switch_writes_nothing`, `tests/contract/acp/test_model_row.py`)
- [x] reattach (test: `tests/e2e/acp/test_acp_daemon.py::test_a_second_editor_process_loads_the_session_on_its_switched_model`)

## Status

Done (2026-10-21).
