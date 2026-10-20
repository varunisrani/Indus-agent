# Task: Store ownership across processes (P17-21) Phase: 17 Owner: wave-27 builder K7

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): no reference code file is named by the
row. Research 03 §3.2 (writable vs read-only opens: read-only never creates, quarantines, migrates
or checkpoints), R03.1 (never live-downgrade WAL), R03.16 (browsing never writes); research 09
R09.21 (bulk maintenance takes the profile's exclusive gateway lock and is refused while the gateway
runs). Our P15 `gateway/daemon/liveness.py` (`gateway.lock` flock), `service/gateway_control.py`
(control client), `cli/commands/sessions_maint.py` (P07-15 verbs), `state/browse.py`.
Evidence: none.

What it does (own words): while a daemon owns the profile it is the one writer of the session
store. A bulk change made by another process at the same time (pruning thousands of rows,
rebuilding the search index, importing an export, restoring a backup over the live file) races
the daemon's turns and can leave rows half-deleted under a running turn or swap the file under an
open connection. So every bulk maintenance verb asks the **maintenance gate** first:

1. **A daemon answers the control socket** (liveness is the socket answer, R06.18): the request is
   sent to the daemon (`maintenance-run`, owner-only), which runs it on its own store handle,
   through the same writer executor as its live turns, so the two are serialised (R09.21). A
   daemon that does not serve the verb (an older build), refuses it, or an operation that only
   makes sense with the daemon stopped (restoring a backup zip over the home) is **refused** with
   a clear message naming `indus gateway stop`; nothing is written locally.
2. **No daemon answers**: the CLI takes the profile ownership lock itself (the daemon's
   single-instance `flock` on `<home>/gateway.lock`, non-blocking) and runs the verb while it
   holds it, so a daemon starting meanwhile cannot claim the profile until the verb is done. If
   the lock is held (a daemon is starting or stopping, its socket is not answering yet, or another
   maintenance run holds it), the verb is refused: "try again in a moment".

Reads stay as they are and are pinned: `sessions list/browse/pinned/stats/export` and the
previews/`--dry-run` open read-only connections (`state.browse.open_readonly`) that never create,
migrate or checkpoint anything (R03.16) — they need no gate and never block on it.

The multi-process stress test runs a "daemon" process (it holds `gateway.lock` and writes turns —
session rows plus user/assistant messages — through the P02 writer), two CLI-style readers (read-only
list + transcript reads), one dashboard-style reader (read-only search + stats), and one process
that runs a WAL checkpoint in the middle; a CLI `sessions prune` attempt during the run is refused
by the gate. Afterwards: every message the writer was told was stored is stored once, in order; no
reader or writer saw "database is locked"; the journal mode every process saw (and the file header
sampled through the run) never changed (R03.1); the store passes its integrity checks.

Inputs → outputs: `MaintenanceGate(scope).run(op, params, local=...)` → `GateResult(where="daemon" |
"local", result)` or `MaintenanceRefused(reason, message)`; daemon side
`MaintenanceVerb(scope).handlers()` → `{"maintenance-run": (handler, seconds)}`.

Files: `src/indus/service/maintenance_gate.py`, `tests/unit/service/test_maintenance_gate.py`,
`tests/integration/state/test_multiprocess_daemon.py`, `tests/integration/state/multiprocess_worker.py`.
Shared: `src/indus/cli/commands/sessions_maint.py` (prune, repair, import through the gate),
`src/indus/cli/commands/sessions.py` (delete through the gate), `src/indus/cli/commands/import_cmd.py`
(restore refused while a daemon owns the profile, lock held otherwise),
`src/indus/gateway/control_socket.py` (`maintenance-run` known and owner-only),
`src/indus/gateway/daemon/side_services.py` (the daemon serves the verb).

Edge cases / failure modes:
- daemon alive but its socket is slow / unreachable → the lock is held by it → refused, never run
  locally (a missed answer is not proof of death);
- stale socket file with no daemon → no answer, lock free → local under the lock;
- the request is too large for one exchange (prune of thousands of ids) → sent in batches;
- a session in use when the daemon deletes it → guarded and reported, as the P02 functions do;
- the daemon answers a refusal (`unsupported`, `forbidden`, `failed`) → refused with its code;
- home not safe (group/world-writable) → the client will not talk to the socket; the lock path
  decides (held → refused);
- previews and `--dry-run` never take the lock or ask the daemon.

Our design differences / decisions:
- Our names: maintenance gate, `maintenance-run` control verb (the JSON-RPC `maintenance.run`
  name is P17-25's; the control socket is the owner-only one-exchange channel every CLI verb
  already uses).
- The reference answers 409 while the gateway runs; we run the verb inside the daemon when it
  serves it, and refuse otherwise.
- Restoring a backup zip (`indus import`) is never routed: it replaces files under the daemon, so
  it is refused while a daemon owns the profile.
- `indus backup` copies databases with the online backup API from a read-only connection: a
  consistent snapshot that never writes the live store, so it is not gated (the plan row lists it;
  recorded below).
- `optimize` (VACUUM) already refuses while any other process holds the store (R03.25); with a
  daemon running that is always the case, so it stays as is.

Tests we will write:
- `test_maintenance_gate.py`: daemon live → `sessions prune` runs inside the daemon (spy on the
  verb; the CLI opens no writable handle); daemon without the verb → refused, nothing deleted; no
  daemon → local under the lock (the lock is held during the run); lock held by someone else →
  refused; `sessions delete` / `repair` / `import` through the gate; `indus import` refused while
  a daemon answers; batches of ids; read-only list/stats/export/browse in an empty home create no
  file.
- `test_multiprocess_daemon.py`: the 60 s (`INDUS_STRESS_SECONDS`) stress run above.

## Decisions for the lead
- `maintenance-run` is an owner-only control verb (added to `KNOWN_VERBS` / `OWNER_ONLY_VERBS`);
  ops `prune` (ids), `delete` (one session chain), `repair` (search index rebuild), `import`
  (a JSON-lines export path). P17-25 may add the JSON-RPC `maintenance.run` beside it.
- `indus backup` is not gated (read-only online backup); backups still include the ledger tables
  (R03.21 follow-up from wave 1).
- The CLI holding `gateway.lock` during a local maintenance run makes a daemon start in that window
  report "already running"; ensure (P17-15) should treat that as "try again" rather than a fault.

Compare checklist (filled after the rewrite; reread research 03 §3.2, R03.1, R03.16, research 09 R09.21):
- [x] R09.21 bulk edit with a live owner goes through the owner (test: `test_maintenance_gate.py::test_prune_with_a_live_daemon_runs_inside_the_daemon`; also delete, repair, import)
- [x] R09.21 refused when the owner cannot run it, nothing written (test: `test_maintenance_gate.py::test_a_daemon_without_the_verb_refuses_and_nothing_is_deleted`)
- [x] R09.21 without an owner the CLI takes the profile lock; a held lock refuses (tests: `test_maintenance_gate.py::test_without_a_daemon_prune_runs_here_holding_the_ownership_lock`, `::test_a_held_ownership_lock_refuses_and_nothing_is_deleted`)
- [x] restore under a live daemon refused (test: `test_maintenance_gate.py::test_restoring_a_backup_is_refused_while_a_daemon_owns_the_profile`)
- [x] R03.16 read-only browsing creates nothing, takes no lock (test: `test_maintenance_gate.py::test_read_only_commands_create_nothing_in_an_empty_home`)
- [x] R03.1 WAL never switched live; no lost rows; no "database is locked" (test: `test_multiprocess_daemon.py::test_daemon_writer_and_readers_share_the_store`)
- [x] R09.21 single deletes stay revision-fenced/guarded and leave tombstones: the daemon runs the same P02 `delete_session` (guarded sessions reported: `test_maintenance_gate.py::test_a_guarded_session_is_reported_by_the_daemon`)
- Deliberate differences: run inside the owner instead of a flat 409; `backup` not gated (read-only
  online backup); `optimize` keeps its holder check.
- Not taken: a JSON-RPC `maintenance.run` on the WebSocket (P17-25 owns the method table).

## Status

Done (2026-10-20). Shared edits: `cli/commands/sessions_maint.py`, `cli/commands/sessions.py`,
`cli/commands/import_cmd.py`, `gateway/control_socket.py` (`maintenance-run` known, owner-only),
`gateway/daemon/side_services.py` (the daemon serves it), `docs/internals/traps-ledger.md` (R03.1,
R03.16, R09.21).

## Integration (P17-15..22 integrator, 2026-10-20)

`maintenance-run` is registered beside K9's attach verbs in `DaemonBridge.control_handlers()`;
peer verification pinned by `test_maintenance_gate.py::test_an_unverified_peer_cannot_run_maintenance`
(registered without the owner flag, as the daemon does: unverified → `forbidden`). End to end
against a real daemon: `sessions delete` runs inside it and `indus import` is refused
(`tests/integration/cli/test_attach_daemon.py`).
