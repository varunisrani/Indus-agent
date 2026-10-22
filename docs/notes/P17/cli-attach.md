# Task: CLI attaches by default (P17-18) Phase: 17 Owner: wave-27 builder K7

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/gateway_chat.py` (all:
refused options and where each lives now, bypass launch rule, resume policy check, finite vs
interactive, detach on Ctrl-C), `hermes_cli/gateway_chat_commands.py` (the slash subset and its
help text), `hermes_cli/gateway_chat_view.py` (pending/unknown display, `/discard`, the finite
"blocked" detach with exit 3, interrupt and exit-code tables). Research 04 §3.4.6, §3.4.8; trap
R04.1, R09.11. Closed before writing.
Evidence: none.

What it does (own words): `indus`, `indus chat`, `chat -q` and `indus -z` no longer build an agent in
this process by default. They make sure the profile's daemon runs (`ensure`, P17-15), connect to it
over the attach channel (P17-16) and drive a `RemoteSessionService` (P17-17): the daemon runs the
tools and writes the store, the CLI shows events and sends input. `--local` keeps the in-process
route of P06 exactly as it is today. A run that injects its own model client (the test seam
`HostOverrides`) is in-process too: such a client cannot reach a daemon.

Rules in attach mode:
- **Refused flags** (exit 2, nothing runs, the message names `--local` and where the capability
  lives): `-w/--worktree` (creating a worktree is local work), `--no-restore-cwd` (the daemon keeps
  the session's frozen folder; `--in DIR` picks one for a new session), `--voice` (recording and
  speech-to-text happen in this terminal), `--save-trajectories` (written by the local agent after
  each turn). Nothing ever falls back to a local agent (R04.1).
- **Bypass launch** (`--safe-mode`, `--ignore-user-config`, or their env vars) for a new session
  must name `--model` and `--provider` (P17-09 `check_launch`): exit 2 with an example.
- **Frozen policy**: a new session sends its creation policy (cwd, model, provider, reasoning,
  toolsets, turn cap, skills, switches; launch env vars become fields); a resume sends only the
  flags given and the daemon refuses a different value (`policy_conflict`) → exit 2. A local
  read-only pre-check gives the same answer before connecting.
- **Session selection** reads the store read-only (ids, titles, `latest`, breadcrumb); the CLI never
  writes the store in attach mode.
- **Finite runs** (`-z`, `chat -q` with `--oneshot`/`-Q`/no terminal): submit once, wait, print as
  the in-process tables do (`oneshot_exit_code`, `finite_exit_code`). A session whose queue is
  paused behind a lost (unknown) turn, or a query that ends up waiting behind one, exits **3** and
  nothing is resent. A lost transport while our own turn runs gives "outcome unknown, do not
  resend" (exit 1 for `chat -q`, 2 for `-z`); the journal entry stays for an explicit retry.
  Ctrl-C interrupts our turn (its generation), waits up to **10 s** for it to settle, then exits 130
  ("detached; accepted work continues in the daemon" when it did not settle).
- **Interactive**: the same classic REPL and renderer (`run_repl`) over an `AttachedHost` (a
  blocking ReplHost on its own loop thread). Slash subset: `/help`, `/quit` (`/exit`, `/detach`),
  `/stop`, `/discard <admission_id>`; approvals and questions use the REPL's cards; every other
  slash or `!` line is answered "not available while attached; use --local". Unknown admissions
  are listed at start with their `/discard` line.
- **Detach**: closing the terminal or `/quit` only detaches (`aclose`); the daemon finishes accepted
  work. Never an interrupt on the way out.
- `/discard <id>` looks the row up in the session's pending list and sends its generation; a stale
  generation (the row moved on) is refused with a message.

Inputs → outputs: global flags + `--local` → `attach.run_attached_chat` / `attach.run_attached_oneshot`
→ exit codes 0/1/2/3/130 and the same stdout contract as in-process.

Files: `src/indus/cli/attach.py`, `tests/unit/cli/test_attach.py`, `tests/unit/cli/_stubs_k7.py`
(stand-in for K6; at integration it became the test double `tests/unit/cli/attach_double.py`). Shared: `src/indus/cli/chat.py` (route choice), `src/indus/cli/oneshot.py`
(route choice for `-z`), `src/indus/cli/app.py` (`--local` flag), `src/indus/cli/args.py`
(`GlobalArgs.local`).

Edge cases / failure modes:
- daemon cannot be ensured (start failed, budget spent, planned stop) → exit 1, one line, "nothing
  ran in this terminal; use --local"; never a local agent.
- the attach implementation is missing from the build → same exit 1 line.
- refused flags combined → all named in one message, exit 2, nothing ensured.
- `-c`/`-r` miss → the selection error, as in-process (exit 1).
- resume with a changed frozen flag → exit 2 (local pre-check or daemon `policy_conflict`).
- unknown admissions before a finite submit → exit 3, nothing submitted.
- the finite wait reports "blocked behind unknown" → exit 3, input retained by the daemon.
- transport lost mid-turn → outcome unknown, exit 1 / 2, no resend.
- Ctrl-C while waiting → interrupt by generation, 10 s settle, 130.
- `/discard` with an id not pending as unknown, or a stale generation → refused, nothing changes.

Our design differences / decisions:
- `--local` instead of refusing everything (D-16.10). A `HostOverrides` run is in-process.
- `--image` / `--verbose` / `--run-budget` / `--compact` are not flags of our CLI, so they are not in
  the refused table; `--voice` and `--save-trajectories` are (they need this process).
- Slash subset is smaller than the reference's (`/model`, `/branch`, `/compress`, `/yolo`,
  `/title`, `/undo`, `/retry`, `/new`, `/usage`, `/tools` need daemon mutation verbs P17-25 has
  not declared yet): they answer "not available while attached".
- `-z` attached sessions are created with source `cli` (the daemon's creatable sources have no
  `oneshot`); `-z` asks the daemon for an unattended turn (`submit(..., unattended=True)`).
- No model alias resolution on the client: our resolver has no aliases, values freeze as typed
  (D-16.424).
- `--local` keeps the P02 per-session file lock; the daemon's host takes the same lock
  (D-16.426), so a `--local` run and the daemon exclude each other on one session. Moving
  `--local` onto the durable lease is a follow-up (it changes `SessionService`).

Tests we will write (`tests/unit/cli/test_attach.py`, against the K6 stand-in):
- each refused flag exits 2 and names `--local`; nothing ensured;
- `--local` (and a `HostOverrides` run) never ensures (the P06 suite stays green as is);
- attach is the default for chat, `-q` and `-z`; ensure failure exits 1 and runs nothing locally;
- finite `-q`/`-z` reply and exit codes over the stand-in; usage file written;
- unknown admission pending → exit 3 before any submit; wait blocked behind unknown → exit 3;
- transport lost mid-turn → outcome unknown, no second submit;
- resume policy conflict → exit 2; bypass launch without model/provider → exit 2;
- interactive: the REPL host feeds events, `/discard` with a stale generation refused, `/stop`
  interrupts by generation, unknown slash answered, closing only detaches (no interrupt).

## Decisions for the lead
- The K6 interface this slice assumed is listed in `tests/unit/cli/_stubs_k7.py`'s docstring and
  in `attach.py` (`AttachBackend`, `AttachedService`): ensure loaded by name from
  `indus.gateway.daemon.ensure:ensure` (the CLI may not import the gateway; precedent: `send` in
  `app.REGISTRY`), the remote host from `indus.service.remote.host:RemoteSessionService.connect`.
  Failure reasons on exceptions: `.reason` (or ProtocolError `data["reason"]`)
  `outcome_unknown`, `blocked_by_unknown`, `policy_conflict`, `stale_generation`.
- `submit(..., unattended=True)` for `-z` and `pending(session_id)` / `discard_unknown(session_id,
  admission_id, generation)` are needed on the remote host (protocol rows for P17-25).
- `--local` is a top-level flag repeated on `chat` (`GlobalArgs.local`); the TUI slice (K8) reads
  the same field for `indus --tui --local`.

- A finite run whose turn asks a question (approval, clarify) nobody here can answer detaches
  without cancelling and exits 3 (found in the compare step; the reference does the same).
- `--local` / `GlobalArgs.local` are added by this slice (`app.py`, `args.py`).

Compare checklist (filled after the rewrite; reopened `gateway_chat.py`, `gateway_chat_commands.py`,
`gateway_chat_view.py`):
- [x] refused flags each named with where they live, nothing started (test: `test_attach.py::test_each_refused_flag_exits_2_and_names_local`)
- [x] bypass launch needs a route (test: `test_attach.py::test_a_bypass_launch_without_a_route_is_refused`)
- [x] resume policy refusal is exit 2, locally and from the daemon (tests: `test_attach.py::test_a_changed_frozen_flag_on_resume_exits_2`, `::test_the_daemons_policy_conflict_exits_2`)
- [x] unknown blocks a finite submit, exit 3, nothing submitted (test: `test_attach.py::test_an_unknown_turn_blocks_a_finite_query_with_exit_3`); queued behind it later → 3 (`::test_a_query_queued_behind_a_lost_owner_exits_3`)
- [x] own lost turn: outcome unknown, never resent (test: `test_attach.py::test_transport_lost_mid_turn_is_outcome_unknown_and_never_resent`)
- [x] `/discard` looks up the lost row and sends its generation; stale refused (test: `test_attach.py::test_discard_with_a_stale_generation_is_refused`)
- [x] Ctrl-C interrupts by generation, 10 s settle, then detach (tests: `test_attach.py::test_ctrl_c_interrupts_by_generation_and_waits_to_settle`, `::test_a_turn_that_does_not_settle_detaches`)
- [x] gap found: a finite viewer with an open question would wait for ever — now detaches with exit 3 (test: `test_attach.py::test_a_question_nobody_can_answer_detaches_a_finite_run_with_exit_3`)
- [x] gap found: an interrupted turn without a reply exits 130 silently — now one stderr line saying it was not run again (test: `test_attach.py::test_an_interrupt_without_a_reply_says_it_was_not_run_again`)
- [x] gap found (ours): a Ctrl-C the OS delivers to another thread was never seen by a main thread blocked on a future — the waits now run in 0.1 s slices (same Ctrl-C tests)
- Deliberate differences: `--local` instead of refusing everything (D-16.10); smaller slash subset (the
  session mutation verbs are not declared on the wire yet); `-z` sessions are created as `cli`; the
  "Session:" line is printed at the end as in-process (`session_id:` on stderr), not at the start.
- Not taken: `--create-if-missing`, `--image`, `--run-budget`, `--compact`, `--format stream-json`
  (not flags of our CLI); the resume recap of earlier messages in attach mode (needs a snapshot
  read from the daemon); ending the REPL at once when the daemon goes away (needs a "channel lost"
  signal from P17-16) — follow-ups.

## Status

Done against the K6 stand-in (2026-10-20). Integration: delete `tests/unit/cli/_stubs_k7.py`, point
`attach.ENSURE_ENTRY` / `REMOTE_ENTRY` at K6's real names if they differ, and either give
`RemoteSessionService` the calls listed in the stand-in's docstring or wrap it in an adapter
(`attach.backend_factory`). Shared edits: `cli/app.py`, `cli/args.py`, `cli/chat.py`,
`cli/oneshot.py`, `docs/internals/traps-ledger.md` (R04.1, R09.11),
`docs/internals/acceptance-map.md` (ACC-I55-2, ACC-K51-1).

## Integration (P17-15..22 integrator, 2026-10-20)

- Stand-in removed. `attach.backend_factory` builds `cli/attach_remote.DaemonBackend` (no loading by
  name: the CLI may import `indus.service`): `ensure` = `service.gateway_ensure.ensure(home, env,
  mode="start")` (a verdict that is not attachable, or a daemon without `ws_url`, is
  `AttachUnavailable`, exit 1); `connect` = `Channel(home_dialer(home, env=..., budget=RecoverBudget()),
  surface="cli")` + `RemoteSessionService(channel, Journal(home)).start()`, wrapped in
  `RemoteAttached` (the `AttachedService` calls). `_stubs_k7.py` became the double of the CLI's own
  seams, `tests/unit/cli/attach_double.py` (no `# STAND-IN` marker).
- K7's assumed extras mapped: `create(..., source, policy)` sends what `session.create` carries
  (`cwd`, `model`, `toolsets`); any other given frozen field (provider, reasoning, switches and
  their env vars, turn cap, skills) is refused before ensure, exit 2 naming `--local` (D-16.490).
  `resume(..., policy)` sends the id; the read-only frozen check runs first. `submit(...,
  unattended)` drops the flag (a finite run detaches with exit 3 on any question). `wait` returns
  the reply streamed after the turn's last tool call (the daemon emits no `message.complete`);
  `OutcomeUnknown` → `outcome_unknown`, or `blocked_by_unknown` when our admission is still queued
  behind an unknown one. `pending` reads the runtime ledger through a read-only connection;
  `discard_unknown` is the new daemon method `admission.discard` (D-16.496). Ctrl-C interrupts only
  our own turn's generation (`own_generation`), never a turn another surface started (D-16.499).
- Tests: `tests/unit/cli/test_attach_remote.py` (the real backend over the daemon's real
  WebSocket endpoint on loopback: `chat -q`/`-z`, exit 3 behind a lost turn then `/discard`),
  `tests/integration/cli/test_attach_daemon.py` (a real `indus gateway run`: CLI attach, a second
  client watching live, the TS client through `indus gateway grant`, maintenance through the
  daemon, cold start by the CLI, `--local` unchanged).

## Wave 46 (F-5: attached `chat -q` with no terminal agrees with `--local`)

Read: none from the reference (rehearsal finding F-5, `.agents/wip/w44/w44-r2.patch`).

- Before: `--local` finite `chat -q` (`--oneshot`, `-Q` or no terminal) runs with `run_context`
  `single_query`, so a risky command is refused by `approvals.single_query_mode` (deny by default);
  attached, the approval request was left waiting ("Resume the session in a terminal") and the run
  exited 3.
- Now (lead's choice: refuse, matching `--local`): the attached finite `chat -q` answers each
  approval request itself with `one_shot_answer(scope)`: `deny` with `ONE_SHOT_DENY` (the same
  wording and setting name `--local`'s policy gives the model) unless
  `approvals.single_query_mode: approve`, then `once`. The turn goes on and the run ends with the
  usual finite exit code. Other questions (clarify, sudo, secret) still detach with exit 3 and the
  resume hint. `-z` is unchanged (its input is `unattended`, auto-approved by the daemon).
- Tests: `tests/unit/cli/test_attach.py::test_an_approval_in_a_finite_chat_is_refused_as_local_does`,
  `::test_single_query_mode_approve_approves_once_as_local_does`; the exit-3 test now uses a clarify
  request. Fail-first: on main the approval case exits 3.

### Decisions for the lead
- W46-f-3 (D-16.1010): the client answers from the profile's config as this process reads it (the daemon reads
  the same profile file). The fuller fix is D-16.487: carry a single-query flag on `prompt.submit`
  so the daemon's turn itself runs `single_query` (floors, allowlist and smart mode then apply the
  same way, and clarify/sudo also follow `--local`). Recorded as the follow-up; the client-side
  answer is safe meanwhile (it only ever denies, or approves when the person configured approve).
- F-5 row in the rehearsal log can move to Done with this (the smoke accepted either outcome; the
  integrator owns `rehearsal-log.md`).
