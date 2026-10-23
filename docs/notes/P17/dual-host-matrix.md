# Task: Dual-host test matrix (P17-26) Phase: 17 Owner: wave-28 builder L3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — the row's reference column is
"— (our suites)": the matrix runs our own P06, P12 and P15 suites against our two hosts. Ours read:
`tests/integration/cli/test_oneshot_exit_codes.py` (P06), `tests/integration/tui_bridge/
test_bridge_child.py` (P12), `tests/e2e/gateway/test_demo3.py` (P15), `tests/integration/cli/
test_attach_daemon.py` and `tests/integration/gateway/spool.py` (the daemon-in-a-subprocess pattern),
`apps/tui/src/app/testRuntime.ts`, `apps/tui/e2e/harness.ts`, the P17 notes and LEAD-NOTES
"P17-15..22 landing notes" (what P17-26 owes: REPL over the real remote host, TUI steer generation,
attach-mode "restarting" wording).
Evidence: none.

What it does (own words): every surface can run its agent in two places — in its own process tree
(`--local` CLI, the TUI's `tui-bridge --stdio` child) or in the profile's shared daemon (the default
since P17-18/19). A person must not be able to tell which, so each surface suite runs against both.
`session_host` is a pytest fixture parametrised over `inprocess` and `attached`; the attached rig
starts a real `indus gateway run` subprocess in a temp profile whose model is the scripted fake on a
loopback port. The CLI cases run the real `indus` (`--local` vs default attach); the TUI-protocol
cases speak JSON-RPC to the bridge child over stdio or to the daemon over its WebSocket (fresh
single-use ticket per dial). Every check goes through `HostRig.same(case, facet, got, want)`: the
expected value is what a person sees in process today; a difference on either host fails unless
`tests/fixtures/service/dual_host_allowlist.yaml` names it with a D-id, and an allow-listed facet
that no longer differs fails as stale. The TUI half (`apps/tui/src/test/hosts.ts`) is the same idea
in vitest: the real `App` + `AppRuntime` linked to the stdio child or to the daemon through the
grant helper, `describe.each` over both.

Inputs → outputs: `session_host` → `HostRig {kind, profile, model, daemon?}`; `rig.use(script)`;
`rig.indus(*args)` → finished process; `rig.peer()` → `StdioPeer | WsPeer` (`call`, `wait_for`,
`events`, `answer`, frames normalised: a daemon server request `{request, replayed, generation}`
flattened); `rig.same(...)` → `MatrixDifference` on an unlisted difference. vitest:
`startHost(kind, turns)` → `{runtime, client, model, home, dispose}`.

Files: `tests/integration/service/conftest_hosts.py`, `tests/integration/service/
test_dual_host_matrix.py`, `apps/tui/src/test/hosts.ts`, `apps/tui/src/test/hosts.test.tsx`,
`tests/fakes/daemon.py`, `tests/fixtures/service/dual_host_allowlist.yaml`. Shared (fixes the matrix
found, below): `src/indus/service/daemon_host.py`, `src/indus/service/session_service.py`,
`tests/unit/service/test_daemon_host.py`, `apps/tui/src/app/appRuntime.ts`,
`apps/tui/src/app/recovery.ts`, `apps/tui/src/app/testRuntime.ts`, `apps/tui/src/app/
appRuntime.test.ts`, `apps/tui/src/entry.tsx`, `apps/tui/src/state/turnStore.ts`,
`apps/tui/src/submit/submission.ts` (+ test), `apps/tui/src/slash/localCommands.ts`.

Edge cases / failure modes:
- a daemon that never mounts its endpoint, or exits → the rig fails with its log;
- every process a rig starts is stopped at teardown; only processes whose environment names the
  rig's own temp home are ever signalled (no pattern kill); a leftover fails the test;
- daemon stream pieces are joined within 33 ms (D-16.367): stream text is compared joined;
- the daemon answers `prompt.submit` with the admission id as `turn_id`: cases follow the turn by its
  `turn.started`;
- an allow-list entry without a D-id or reason, or listed twice, is refused at load.

Our design differences / decisions:
- Demo 3's messaging surface lives in the daemon either way: its `inprocess` runs chat turns
  directly on the daemon's service (P15, `shared_host=False`), `attached` admits them into the
  shared session host every attached surface uses (P17-22). The Demo 3 tests are re-run unchanged.
- The P12 bridge suite is mirrored through the TUI's wire (the bridge child vs the daemon socket),
  not through the in-process `Rig`, whose tests reach into the bridge's internals.
- The seeded difference drops `session.usage` from the attached peer: the matrix fails on the
  turn's event shape.

Tests we will write: the P06 finite-run cases (stdout/stderr contract, exit codes 0/1/2/130, usage
file, hardline and approvals under `-z`/`-q`, resume chain, `-c` miss), the P12 cases (Demo 2 turn,
client lost mid-turn then resumed, settings read at start, the turn's event shape), Demo 3 (one agent
on three platforms, approval by a Telegram button) under both; the seeded difference; allow-list
validation; vitest: a prompt streams and settles with usage, an approval card answered from the
keyboard — on the stdio child and on the daemon.

## What the matrix found, and what was done

Fixed (small, P17 code; each pinned by a test that fails without it):
- **The daemon sent no `message.complete` / `session.usage`.** An attached TUI never settled a turn
  (its controller goes idle on `message.complete`) and showed no usage. `DaemonSessionHost` now adds
  the bridge's turn-end pair after `turn.done` through the event log (live and replayed), reason
  from the turn's own `turn.done` (`test_daemon_host.py::test_a_turn_ends_with_its_final_message_and
  _the_usage`, `::test_an_interrupted_turn_ends_with_the_interrupt_reason`; matrix
  `bridge.event_shape`; vitest `hosts.test.tsx`).
- **Sessions created through the daemon were stored with source `gateway`.** `indus -c` / `--resume
  latest` after an attached run said "No earlier session to continue in this workspace" and
  `sessions list` showed `gateway`. `SessionService.create(params, *, source=)` stores the creating
  surface (`cli`, `tui`, `gui`, `acp`, `web`); the gateway's own sessions keep the service's source
  (`test_daemon_host.py::test_a_surface_session_is_stored_under_that_surface`; matrix
  `cli.z.resume`, `bridge.event_shape/stored source`).
- **The TUI's steer named no generation** (owed to P17-26): the daemon fences every control by
  the turn it names, so an attached TUI's `/steer` and busy-mode steer were refused
  (`INVALID_PARAMS`) and fell back to queueing. Both now send the running turn's generation
  (`turnStore.steerParams`; `submission.test.ts::steer: names the running turn's generation (the
  daemon fences controls by it)`; vitest `hosts.test.tsx::/steer while a reply runs is handed to it`
  fails on the daemon without it; matrix `bridge.steer`).
- **TUI "restarting" wording in attach mode** (owed to P17-26): a lost attach link says "Lost the
  connection to the gateway daemon; reconnecting and reopening your chat…", and a link lost while a
  reply ran adds, carried into the reopened session, "…its outcome is unknown and it was not sent
  again (do not resend it)" (`appRuntime.test.ts::attached: a link lost mid-reply says the outcome
  is unknown and sends nothing again`). `RuntimeOptions.attached` set by the entry from the transport
  plan.

Allow-listed (D-ids for the lead; details in the YAML):
- `cli.z.approvals/outcome` (D-16.490): attached `-z` sends no `unattended`, so a dangerous command
  makes the finite run detach with exit 3 instead of being approved for the run.
- `cli.q.denies/outcome` (D-16.487): a finite attached `chat -q` that meets a question detaches with
  exit 3 instead of denying it. Removed at the waves 45 + 46 integration (D-16.1025): wave 46's F-5
  fix (D-16.1010) makes the attached client deny like the in-process host, so the entry was stale.
  Since wave 47 (D-16.1051 – D-16.1053) the daemon itself runs a finite attached turn single-query
  (`prompt.submit.single_query`), so both hosts decide on the same `--local` route; the client-side
  answer is gone and no `cli.q` entry is needed.
- `bridge.lost_mid_turn/lost turn finished` (D-16.10): closing an attached client only detaches; the
  daemon finishes the turn.
- `bridge.event_shape/session events` (D-16.550): no `session.info` after create/resume, no
  `session.title` when the title lands. Removed in wave 48 (W48-h): the daemon host sends both to
  the session's viewers (`DaemonSessionHost.announce_info`, the turn-end title check), so the
  entry was stale.
- `bridge.settings/settings` (D-16.549): `config.set` is not served by the daemon (-32601).
  `model.options`, `complete.slash` and `session.list` are served since wave 47
  (D-16.1038, D-16.1039); `config.get`, `complete.path` and `commands.catalog` since wave 48
  (W48-b), so only the write differs. `slash.exec` / `command.dispatch` are served since wave 48
  too (W48-h; the matrix has no slash case). Removed in wave 50 (W50-a): the daemon serves `config.set`
  with the bridge's checks (`service.config_writes`), so the case agrees on both hosts and the
  entry was stale.

## Decisions for the lead
- D-? (proposed): the daemon serves the session methods only; the TUI's settings/picker methods
  (`model.options`, `config.*`, `commands.catalog`, `complete.*`, `session.list`, …) answer -32601
  when attached. Proposed change: serve the read-only ones from the daemon over the same dispatcher
  (`gateway/ws/methods.py`), or let the attached TUI read them from a local read-only helper.
- D-? (proposed): no `session.info` / `session.title` from the daemon. Proposed change: the WS
  `session.create`/`resume`/`subscribe` handlers send `session.info` for the session (the bridge's
  `session_info` shape, from `DaemonSessionHost`), and the host emits `session.title` into the
  session's event log when a title is stored (the same hook that feeds `session.updated`).
- Not allow-listed because it does not show on the matrix's facets, but seen: the daemon's
  `prompt.submit` answers `generation: 0` when the input is admitted while the session is idle and
  the worker has not claimed it yet (`gateway/ws/methods.py::submit`); clients follow the turn by its
  `turn.started`, so nothing breaks today. Proposed: answer the claimed generation (wait for the
  claim when the queue was empty) or leave `generation` out when not yet known (P17-25 schema).
- The attached `-z` gap (D-16.490): P17-25 should carry `unattended` on `prompt.submit` and the
  daemon should apply the run-scoped auto-approve (floors still apply) so `-z` behaves the same on
  both hosts; then remove the allow-list entry.
- The matrix files are slow (real daemons): ~3.5 min for 39 cases; marked `slow`.

Compare checklist (filled after the rewrite; no reference file — compared against our own suites):
- [x] P06 suite under both (tests: `test_dual_host_matrix.py::test_z_prints_only_the_reply`, …,
  `::test_continue_miss_exits_1`)
- [x] P12 bridge suite under both (tests: `::test_bridge_demo_turn`, `::test_bridge_event_shape`,
  `::test_bridge_client_lost_mid_turn_then_resumed`, `::test_bridge_steer_reaches_the_running_turn`,
  `::test_bridge_settings_the_tui_reads_at_start`)
- [x] P15 Demo 3 under both (tests: `::test_demo3_one_agent_on_three_platforms`,
  `::test_demo3_approval_by_a_telegram_button`, `::test_demo3_cron_delivery_on_its_tick`,
  `::test_demo3_chunker_busy_guards_and_default_deny`)
- [x] seeded difference fails (test: `::test_a_seeded_difference_fails_the_matrix`)
- [x] TUI under both (vitest: `apps/tui/src/test/hosts.test.tsx`)
- Deliberate differences: the five allow-list entries above.
- Not taken: the in-process P12 `Rig` contract tests (they assert bridge internals, not the wire);
  Demo 3's SIGKILL-mid-send case (it runs its own daemon child, built without the option).

## Status

Done (2026-10-20). Python matrix 51 cases passed (both hosts; Demo 3 under both); vitest
`hosts.test.tsx` 6 passed (stdio child and daemon); full `pnpm test` 908 passed.

Integration (2026-10-20): D-16.547 (matrix), D-16.548 (turn ends, stored source); allow-list
`bridge.settings` → D-16.549, `bridge.event_shape/session events` → D-16.550; the loader now
refuses an unnumbered `D-?`. With `unattended` on `prompt.submit` and the daemon's run-scoped
auto-approve (D-16.554) `cli.z.approvals` agrees on both hosts and its entry is gone. The
daemon's `prompt.submit` no longer answers `generation: 0` (D-16.555). `usage_numbers` has one
owner, `service.session_service` (D-16.556).
