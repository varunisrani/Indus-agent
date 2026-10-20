# Task: ACP editor policy on the daemon — `session.mutate` and `acp.editor_policy` (daemon half of P19-04/05/06/12/13) Phase: 19 Owner: wave-31 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/session_local_mcp.py` (editor
MCP servers as gateway-owned session state: fingerprint, frozen manifest, per-session scope, fork,
re-check at a turn boundary), `gateway/session_mutations.py` (the `session.mutate` writer: which
operations need idle, the mutation lock, deferral of a model pick on a busy session, projections
after a commit), `gateway/session_deferred_pick.py` (a pick made mid-turn lands at the next claim),
`gateway/session_mutation_model.py` (first 60 lines: how a pick resolves its route),
`gateway/session_policy.py` (first 200 lines and the `editor` mentions: the frozen launch policy
holds an `editor_mcp_json` record only). Our side: `docs/plan/phases/P19-acp-mcp-serve.md` rows
P19-03..06, 12, 13; `docs/plan/16-what-we-change.md` D-16.600..609; LEAD-NOTES "P19-01..07 landing
notes"; `.agents/wip/w30/README.md` (w30-a2) and the a2 patch's `acp/{attached_host,backend,
local_host,toolset,model_mode,commands}.py`; `src/indus/acp/{attached_host,local_host,backend,
sessions,history}.py`; `tests/fakes/daemon.py` (`AcpDaemon` shim); `gateway/ws/methods.py`;
`service/{daemon_host,session_service,mcp_host,creation_policy}.py`; `mcp/discovery.py`
(`McpHost`); `tools/registry.py`; `tui_bridge/methods_session.py` (title/undo/branch);
`protocol/attach.py` + codegen. Closed before writing.
Evidence: none.

What it does (own words): an editor attached to the profile's daemon (`indus acp`, the default
mode) can now create sessions there. The daemon advertises `acp.editor_policy` and serves one new
attach method, `session.mutate {session_id, operation, payload, request_id, generation?}`, for a
session this connection attached. Operations:

- `editor_mcp {mcp_servers, edit_approval_policy}` — the editor's own MCP servers for this session
  only. Specs (command, args, env, headers, URL) stay in the daemon's memory; the session row keeps
  a record (`model_config["editor_policy"]`): a fingerprint per server (env and header values
  hashed first) and the tool schemas of the first listing. A server whose resent spec differs from
  its record is refused (nothing changes); new names join the record. The servers connect through
  the P13 client (`McpHost`) into a registry of that session alone (`SessionRegistry`: the shared
  registry plus the session's own tools), named `editor-<name>`, toolset `mcp-editor-<name>`; no
  other session — not even one that selects every toolset — sees them. Idle: the agent is rebuilt
  now; a turn running: the change waits for the turn boundary (`deferred`).
- `branch {mcp_servers?}` — fork: a stored copy of the session (idle only); the child's editor
  servers are the ones named, else a copy of the parent's specs; its record is written at once and
  the servers connect when the child is attached.
- `workspace {cwd}` — move the session's folder (idle only): the row's cwd, and the agent rebuilt
  in the new folder.
- `model {model, provider?, confirm?}` — switch the session's route (never the profile default).
  A target whose window is smaller than the context in use answers `confirm` (nothing changed)
  until the call says `confirm: true`. A turn running: kept for the next turn boundary
  (`deferred`); a pick that fails there is announced as a warning notice.
- `rewind {count}` / `reset {}` — take back user turns / empty the conversation (idle only); the
  agent is rebuilt from the store.
- `rename {title}` — store the title (the row is written now if the first reply has not come yet)
  and send `session.title`.
- `compress {focus?}` — the manual compression between turns.
- `command {name, arg}` — the information commands an editor may run (`tools`, `status`, `usage`,
  `version`), answered as `text`.

Fencing: every operation runs under the session's mutation gate; the daemon's turn runner waits at
the same gate before it starts a claimed turn and applies deferred changes there, so an agent is
never rebuilt under a running turn and a turn never starts on a half-made change. An operation that
needs idle answers `SESSION_BUSY` while a turn is running or claimed. `generation` (optional) names
the turn the client believes runs: another running turn answers `refused` / `stale_generation`.
Idempotency: the same `request_id` for the same session and operation answers the first result
(also while the first call is still running); reused for something else → `IDEMPOTENCY_CONFLICT`.

ACP sessions on the daemon also get the editor toolset when the client names none
(`platform_toolsets.acp` minus `no_mcp`, else `indus-acp`), and a resumed ACP session runs in its
stored folder.

Inputs → outputs: `session.mutate` params → `{status: applied|deferred|refused|confirm, …fields}`
(`session_id`, `cwd`, `title`, `model`, `provider`, `message`, `reason`, `removed`, `text`,
`servers`, `refused_servers`, `changed`, `outcome`); `runtime.describe`/`describe` capabilities gain
`acp.editor_policy`.

Files: new `src/indus/service/session_mutations.py`, `src/indus/service/editor_mcp.py`,
`src/indus/tools/session_registry.py`, tests (`tests/unit/service/test_session_mutations.py`,
`tests/unit/service/test_editor_mcp.py`, `tests/unit/tools/test_session_registry.py`,
`tests/contract/gateway/test_ws_session_mutate.py`; `tests/e2e/acp/test_acp_daemon.py` rewritten).
Shared: `protocol/attach.py` (+ models, row), generated protocol package, `gateway/ws/methods.py`
(handler), `service/daemon_host.py` (gate, capability, close), `service/session_service.py`
(`agent_shaper` seam), `service/rpc/dispatch.py` + `service/remote/channel.py` (deadlines),
`tests/fakes/daemon.py` (shim removed), protocol samples/catalogue tests, the old e2e.

Edge cases / failure modes:
- a spec without a name, more than 16 servers, a payload of the wrong shape → `INVALID_PARAMS`
  (the message never echoes a spec: argv and env may hold credentials);
- the same server name twice in one list: the later one wins;
- an editor server that fails to connect: its tools are absent, the others still come;
- the daemon restarts: specs are gone, the record stays; the editor's reconnect resends them —
  identical → connected again, changed → refused;
- a session with a record but no specs yet (resumed by someone else): no editor tools until the
  editor resends;
- the session ends (last ACP viewer gone) or the daemon closes: its editor servers are closed;
- a rotating compaction moves the session: the host key stays, so the gate and the servers follow;
- `command` outside the four → `refused` / `unsupported_command`;
- retry of a branch with the same `request_id` → the same child, never a second copy.

Our design differences / decisions:
- one isolated registry per editor session instead of per-session tags in the tool names;
- deferral covers editor MCP changes too (not only model picks), applied at the same boundary;
- the record is per server (fingerprint per name), so adding a server later is allowed and only a
  changed spec is refused;
- no durable deferred pick: it lives in the daemon's memory (a restart drops it);
- no drift re-check of a live server's listing at each turn start: a changed listing is reported
  (`changed`) when the editor reconnects the servers.

Tests we will write:
- unit, per operation over a real `DaemonSessionHost` on the fake model: editor_mcp (connect,
  isolation from another session, changed spec refused, record has no secret, deferred while
  running, closed with the session), branch (child + specs, busy refused, idempotent), workspace,
  model (applied, confirm, deferred then applied at the boundary, refused at the boundary → notice),
  rewind, reset, rename (before the first reply), compress, command, stale generation, request id
  reuse;
- unit: `SessionRegistry` (reads merge, writes stay, `all` of another registry never sees them),
  fingerprints and record;
- contract over the WS endpoint: method served, foreign connection → not found, shapes;
- ACP contract suites run against the real method (shim removed);
- e2e with a real `indus gateway run` in `tmp_path`: attached `indus acp` `session/new` + fork over
  ACP frames, a prompt on the fake model and a model switch (through ACP once the adapter serves
  `session/prompt`, else through the attached backend in process), the next request naming the new
  model.

## Decisions for the lead
- The editor record is frozen by the first `editor_mcp` of a session, not by `session.create`
  (create carries no specs; the attached host sends `editor_mcp` right after create).
- A changed spec refuses the whole `editor_mcp` call (status `refused`, reason
  `editor_mcp_changed`, `refused_servers`); servers not named in the record join it.
- Editor tools live in a per-session `SessionRegistry` over the shared one (new
  `tools/session_registry.py`), so they are invisible to every other session including ones that
  select every toolset; the tool names match `--local` (`editor-<name>`).
- `SessionService.agent_shaper` (new optional seam, `(session_id, options, selection) →
  (options, selection)`, run before the factory in attach and reload) is how the daemon gives a
  session its own registry, toolsets and folder.
- Idle-only operations answer the protocol's `SESSION_BUSY` error (the attached host maps it to
  `session_busy`; one small mapping added there, see Status).
- `session.mutate` deadline 600 s on the daemon, 660 s in the client channel (compression is a
  model call; editor servers may take a while to start).
- ACP sessions created over the WebSocket without toolsets get `platform_toolsets.acp` (minus
  `no_mcp`) or `indus-acp`; `no_mcp` itself is not honoured on the daemon yet.
- The model pick is re-applied to the agent's binding before each turn when a rebuild (rewind,
  workspace, a lease-waited reload) lost it; the row's `model` column is not updated.

Compare checklist (filled after the rewrite; reopened `gateway/session_local_mcp.py`,
`gateway/session_mutations.py`, `gateway/session_deferred_pick.py`):
- [x] only fingerprints and schemas durable; specs in memory (test:
  `tests/unit/service/test_session_mutations.py::test_editor_servers_reach_their_session_only_and_the_record_holds_no_secret`,
  `tests/contract/acp/test_session_new.py::test_editor_mcp_env_is_never_written_to_the_store`)
- [x] identical respec after a restart connects; changed respec refused (test:
  `test_session_mutations.py::test_a_changed_spec_is_refused_and_the_same_one_after_a_restart_connects`)
- [x] per-session isolation; a fork never shares the parent's connections (test:
  `test_session_mutations.py::test_editor_servers_reach_their_session_only_and_the_record_holds_no_secret`,
  `tests/unit/tools/test_session_registry.py::test_reads_see_both_sides_and_writes_stay_in_the_session`)
- [x] fork: named servers or a copy of the parent's (test:
  `test_session_mutations.py::test_branch_copies_the_session_with_the_parents_servers_or_the_named_ones`,
  `tests/contract/acp/test_session_load.py::test_fork_gets_the_parents_mcp_servers_or_the_editors_own`)
- [x] idle-only operations refuse while a turn runs; a model pick mid-turn waits for the boundary,
  the running turn keeps its route (test: `test_session_mutations.py::test_idle_only_changes_are_refused_while_a_turn_runs`,
  `::test_a_pick_made_mid_turn_lands_at_the_next_turn_and_survives_a_rewind`,
  `tests/contract/gateway/test_ws_session_mutate.py::test_idle_only_changes_answer_busy_while_a_turn_runs`)
- [x] servers closed when the session ends (its last ACP viewer left) (test:
  `test_session_mutations.py::test_servers_sent_mid_turn_wait_for_the_next_turn_and_close_with_the_session`)
- [x] exact retry returns the first result; a reused id for another change is refused (test:
  `test_session_mutations.py::test_branch_copies_the_session_with_the_parents_servers_or_the_named_ones`,
  `test_ws_session_mutate.py::test_the_daemon_advertises_the_editor_policy_and_serves_the_mutation`)
- [x] gap: a server list larger than 64 KiB was accepted — fixed by `MAX_LIST_BYTES` in
  `valid_specs`, pinned by `test_session_mutations.py::test_a_bad_server_list_is_invalid_and_names_no_spec_value`
- [x] gap: editor servers could be set on a non-ACP session — now `INVALID_PARAMS` (same test)
- [x] a stdio server runs in the session's folder; a move restarts it there (`EditorServers.connect`)
- [x] real daemon end to end: attached `indus acp` new + prompt + model switch + fork + load (test:
  `tests/e2e/acp/test_acp_daemon.py::test_an_attached_editor_session_prompts_switches_model_and_forks_on_the_daemon`;
  run with the w30-a2 patch applied as well, where prompt and the model picker go through ACP)
- Deliberate differences: the reference keys every editor connection by a per-session tag inside
  one shared tool table; we give the session its own registry (same tool names as `--local`). The
  reference keeps a deferred pick durable and re-checks a live server's listing at every turn
  start; we keep the pick in memory and report a changed listing when the editor reconnects its
  servers. The reference fingerprints the whole list; we fingerprint per server so a server can
  be added later. A repeated server name keeps the later spec (the reference refuses the list).
  Editor servers use the P13 connect / call timeouts, not shorter ones of their own.
- Not taken: durable deferred picks; the per-turn schema drift re-check; `no_mcp` on the daemon;
  writing a model switch into the row's `model` column.

## Status

Done (2026-10-20), wave-31 builder D1. Attached `indus acp` works against a real daemon:
`session/new`, fork, load, workspace moves, editor MCP servers; with the w30-a2 ACP handlers also
prompt, `/model` / the model picker, `/undo` `/retry` `/new` `/title` `/compress`. Shared files
(minimal): `acp/attached_host.py` (docstring; `SESSION_BUSY` / `SESSION_NOT_FOUND` from
`session.mutate` map to `session_busy` / `not_found`), `service/session_service.py`
(`agent_shaper`), `service/daemon_host.py`, `gateway/ws/methods.py`, `protocol/attach.py`, deadlines
in `service/rpc/dispatch.py` and `service/remote/channel.py`, `tests/fakes/daemon.py` (shim removed:
`session.mutate` is real, frames recorded, scripted editor servers), traps/acceptance rows naming
the rewritten e2e. Handed to the lead: the a2 wiring of `run_command` to `session.mutate command`
and the a2 test line that pinned the old shim's refusal of `rename`.
