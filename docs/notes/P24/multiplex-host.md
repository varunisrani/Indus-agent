# Task: Multiplex host lock and profile serving (P24-01) Phase: 24 Owner: wave-37 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/host_rendezvous.py` (lock,
record, liveness, publish/clear), `gateway/host_attach.py` (attach decision, identify-only served
set, transient refusal), `hermes_cli/gateway_multiplex_mode.py` (skim: the boot verdict),
`gateway/run_profile_reconcile.py` (serve/unserve/rescan, the 5 s bound and `pending`). Research
06 §3.1 step 2, §3.21. Closed before writing.
Evidence: none.

What it does (own words): with `gateway.multiplex_profiles: true` (default false, D-16.17), one
gateway process per OS user serves every live profile under the root it was launched from. The
winner holds an exclusive `flock` on `$XDG_STATE_HOME/indus/gateway-host.lock` (fallback
`~/.local/state/indus`) for its whole life and publishes a rendezvous record next to it
(`gateway-host.json`: pid, process start time, root, launch home, served profiles, protocol
version). A second `gateway run` for some profile does not start a second gateway: it reads the
record, proves the owner is the same live process (pid + start time, and the host lock is not
free), then asks the owner's control socket `identify`. Only a live `identify` answer from that
pid says what is served: if the profile is served the run hands over and exits 0 (75 under a
supervisor, so the unit is retried rather than parked); if it is not served yet the run asks the
owner to `serve-profile` it and attaches when the answer includes it. A record whose owner is alive
but does not answer within the wait is a transient refusal, always 75 (R06.17: never 78). A dead
or recycled owner (or a free host lock) means the record is stale: the run starts normally.

The owner keeps the served set in `ServedProfiles`: the launch profile is always served and can
never be unserved; `serve-profile` / `unserve-profile` / `rescan-profiles` control verbs change it
under one lock, each answer bounded to 5 s (the work goes on past that, the answer is then
`{"pending": true, "served_profiles": [...]}`). Every change republishes the record. Tickets are
minted for the routed profile only: a book with a served-set check refuses (`wrong_profile`) to
mint or redeem for a profile the process does not serve.

Inputs → outputs:
- `host_state_dir(env)`; `HostLock(dir).try_acquire()/release()/observed_free()`;
  `HostRecord.to_json/from_json`; `publish_record`, `read_record` (owner uid only),
  `clear_record` (only our own).
- `record_alive(record, lock)` → bool; `decide_second_run(profile, record_dir, env=, identify=,
  serve=, wait=, clock=)` → `RunVerdict{outcome: start|attach|refuse, exit_code, message,
  transient}`.
- `MultiplexHost(launch_scope, record_dir, clock=)`: `claim()`, `served_profiles()`,
  `serve(name)`, `unserve(name)`, `rescan()`, `identify_fields()`, `served_ticket_ids()`,
  `control_verbs()` → `{verb: (handler, timeout)}`; `close()`.
- `mint_routed_ticket(book, host, profile, purpose)`.
- control socket: `serve-profile`, `unserve-profile`, `rescan-profiles` known and owner-only.
- `TicketBook(served=callable)`: mint/redeem refuse a profile id outside the served set.
- config row `gateway.multiplex_profiles` (bool, false).

Files: `src/indus/gateway/multiplex/__init__.py`, `src/indus/gateway/multiplex/host.py`,
`tests/unit/gateway/multiplex/test_host.py`,
`tests/integration/gateway/multiplex/test_attach_second_run.py`; shared:
`src/indus/gateway/control_socket.py` (three verbs), `src/indus/service/ws_auth.py` (`served=`),
`src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py`
(`multiplex_profiles`).

Edge cases / failure modes:
- record absent, unreadable, wrong protocol, written by another uid or in a dir we do not own →
  no owner (start); no record but the lock held (an owner between claim and publish) → 75.
- a profile that still runs its own per-profile gateway → `own_gateway`, not served.
- recycled pid (start time differs) or host lock observed free → stale (start), even though the
  pid looks alive.
- owner alive, control socket not bound yet (boot race) → poll `identify` up to the wait, then a
  transient refusal (75).
- identify answers but from another pid → not the owner (treated as no answer).
- serve of an unknown, tombstoned or invalid profile → refused with a code, nothing changes;
  unserve of the launch profile → refused; serve of an already served one → answered as served.
- a serve hook that hangs → the verb answers `pending` at 5 s, the work continues; a later answer
  shows the outcome.
- the record dir is created 0700 and tightened if wider; the record file is 0600.

Our design differences / decisions:
- Our names: `gateway-host.lock` / `gateway-host.json` under `$XDG_STATE_HOME/indus` (the row's
  path); one role only (gateway), no dashboard role and no token file (P18 has its own record).
- The record holds the launch root and home; a record from another root is not our host.
- Off by default (D-16.17): `multiplex_profiles: false` keeps one profile per process; an explicit
  true is the only way in (no "retired opt-out" ladder, no preflight migration).
- Attach verdict: ATTACH 0 at a shell / 75 supervised; TRANSIENT 75 always; a live owner that
  refuses to serve the profile (unknown profile) is a permanent refusal, 78 supervised / 1 at a
  shell; no `--force` side-by-side start and no standalone shim.
- The verbs are owner-only on the socket (they change what the process serves and therefore who
  can mint tickets).

Tests we will write:
- record round trip, foreign uid/dir ignored, stale on recycled pid / free lock.
- second run: attach 0 / 75 supervised; stale record → start; live owner without answer → 75;
  unserved profile → serve then attach; unknown profile → refuse 78.
- verbs: serve/unserve/rescan semantics, launch profile kept, `pending` past 5 s on a fake clock.
- tickets: book with served set refuses mint and redeem for an unserved profile.
- integration: a real owner process (lock + record + control socket) and a second run in this
  process: attach after identify, 75 when the owner never answers, start after the owner exits.

## Decisions for the lead
- Wiring owed: `gateway run` (daemon `command.py` / `process.py`) must call
  `decide_second_run` before the per-home instance lock when `gateway.multiplex_profiles` is on,
  build a `MultiplexHost` after the claim, merge `identify_fields()` into `identity_payload`,
  register `control_verbs()` on the `ControlServer`, and pass `served=host.served_ticket_ids` to
  the daemon's `TicketBook`. Serve/unserve hooks start/stop that profile's adapters (P24-02
  scopes, P24-03 adapter table, P24-04 mounts).
- D-16.17 flip to "multiplex available, off by default" in `16-what-we-change.md` (lead file).
- Exit code for "live owner will not serve this profile": 78 supervised / 1 at a shell (a
  config problem: the profile is not live); the reference parks or starts beside it.

Compare checklist (filled after the rewrite; reopened the four files):
- [x] lock held for life, OS drops it on death; record names pid + start time (test:
  `tests/unit/gateway/multiplex/test_host.py::test_a_record_with_a_recycled_pid_is_stale`)
- [x] a free host lock falsifies a record whose pid looks alive (test:
  `test_host.py::test_a_free_host_lock_makes_a_live_looking_record_stale`)
- [x] foreign-owned record ignored (test: `test_host.py::test_a_record_owned_by_another_user_is_ignored`)
- [x] served set only from a live identify; owner without answer is transient (tests:
  `test_host.py::test_an_owner_that_never_answers_is_a_transient_refusal`,
  `tests/integration/gateway/multiplex/test_attach_second_run.py::test_a_record_without_a_live_answer_refuses_with_75`)
- [x] not served yet → serve request then attach (test:
  `test_host.py::test_an_unserved_profile_is_served_then_attached`)
- [x] identify from another pid is not the owner (test:
  `test_host.py::test_an_identify_answer_from_another_pid_is_not_the_owner`)
- [x] gap found: the reference also requires the answer's home to agree with the record — fixed
  by `_is_owner` (pid and, when named, the launch home), pinned by
  `test_host.py::test_an_identify_answer_naming_another_home_is_not_the_owner`
- [x] gap found: a profile still running its own per-profile gateway must not be served (two
  pollers on one bot) — fixed by the `own_gateway` refusal in `_start`, pinned by
  `test_host.py::test_a_profile_running_its_own_gateway_is_not_taken_on`
- [x] serve/unserve/rescan under one lock, launch profile never unserved, 5 s → pending (tests:
  `test_host.py::test_rescan_answers_pending_past_five_seconds`,
  `test_host.py::test_the_launch_profile_cannot_be_unserved`)
- [x] record dir owner-only and tightened (test: `test_host.py::test_the_record_dir_is_owner_only`)
- [x] only our own record is cleared at exit (test: `test_host.py::test_clear_leaves_another_owners_record`)
- Deliberate differences: no serve/desktop roles, no token file or HTTP identity probe (our
  liveness channel is the control socket); no boot-epoch field (start time from /proc plus the
  host-lock check covers reboot reuse); no legacy create-time fallback (fresh start); no
  standalone shim, `--force`, migrate command or watcher-driven rescan (rescan is on request; a
  periodic rescan is lead wiring if wanted); no per-profile parked map (a profile that fails to
  serve is reported in the verb's answer and not served).
- Not taken: tenant check by root name collision is folded into "record root must equal ours";
  the 30 s watcher rescan and the re-scan of a served profile whose `config.yaml`/`.env` changed
  (a credential added after serving needs `unserve-profile` + `serve-profile`, or the lead wires a
  periodic `rescan()` plus a signature check); `--replace` of the host owner and the SIGTERM
  record cleanup (the daemon's stop path calls `MultiplexHost.aclose`; a crash leaves a record the
  free lock already marks stale).

## Status

Done (2026-10-21). Daemon wiring listed above is owed by the lead.
