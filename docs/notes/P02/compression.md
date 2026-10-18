# Task: compression records — rotation (P02-12) Phase: 02 Owner: P02 wave-4 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_compression.py` (lease
claim helper, `try_acquire/refresh/release_compression_lock`, `publish_compression_child` 278–450
and `_publish_child_session_row`, cooldown and streak counters with snapshot/restore,
`finalize_orphaned_compression_sessions`, chain step and walkers), `hermes_state_pidns.py` (all:
stamp format idea), `hermes_state.py` (`_compression_lock_holder_process_is_dead` 139),
`tests/hermes_state/test_compression_watermark_commit.py` (all),
`tests/hermes_state/test_hermes_state_compression_locks.py` (all). Closed before writing.
Evidence: none.

What it does (own words): compression (P08) summarises a long conversation into a new child
session. The summary call is slow and external, so the store gives the compressor three things.
(1) A lock per session so two processes never rotate the same conversation into two children: a
row with a holder and an expiry; a crashed holder is replaced once its row expires, or earlier when
its process is provably gone. (2) A watermark: the newest live message id when the summary
started. (3) `publish_continuation`: one write transaction that checks the lock is still ours, ends
the parent with `compression`, creates the child, writes the handoff (summary) rows, then copies
every live parent row newer than the watermark behind them — so a message appended while the
summary ran is never summarised away (R03.10). A reader sees either the live parent or a complete
child, never half. Ordinary appends never look at the lock. The module also keeps the anti-thrash
counters the compressor needs across restarts, and chain helpers.

Holder stamps (`state/holders.py`): `holder_stamp()` = `pid=<pid>:pidns=<inode of
/proc/self/ns/pid>:<hostname>` (`pidns=-` when the namespace cannot be read). Callers may append
`:<anything>` (a task nonce) — only the first three fields are parsed. `holder_is_stale(holder,
expires_at, now)`: expired (`expires_at <= now`), or the holder's PID is provably dead: the
stamp parses, the PID is not ours, the host is ours, the recorded namespace equals our own known
namespace, and `os.kill(pid, 0)` raises `ProcessLookupError`. Any doubt (foreign or unknown
namespace, other host, unstamped holder, permission error, non-POSIX) keeps the row until expiry
(R03.20): a false keep heals itself at expiry, a false reclaim lets two compressors fork a lineage.

Inputs → outputs (`state/compression.py`):
- `acquire(store, session_id, holder, *, ttl=300.0) -> bool` — one transaction: stale row deleted
  (only if the same holder is still in it), insert-or-ignore ours, read back. Lock contention uses
  its own 5 s budget; out of budget → False (refuse, never raise). Missing session → False.
- `refresh(store, session_id, holder, *, ttl=300.0) -> bool` — extends only while `holder` is in
  the row (also after expiry, if nobody took it). `release(store, session_id, holder) -> bool` —
  deletes only our row. `holder_of(store, session_id) -> str | None` (live holder, diagnostic).
- `is_locked_in(conn, session_id, *, now) -> bool` — a live lock exists; reaps a stale one (used by
  rewind here and delete in P02-18; must run inside a write transaction).
- `watermark(store, session_id) -> int` — max live message id (0 when none); `watermark_in`.
- `publish_continuation(store, parent_id, child_fields, handoff_rows, watermark, holder, *,
  ceiling=None) -> str` (child id). Steps: lock row holder == ours and `expires_at > now`, else
  `CompressionBusy`; parent ended by an *automatic* or *recoverable* reason (shutdown, reaper) →
  that stale stamp is cleared (the lock holder proves the conversation is alive); ended by
  compression → `CompressionClosed`; ended deliberately → `CompressionParentEnded`; parent ended
  with `compression` (`sessions.end_in`); child created via `sessions.create_in` with the parent's
  source, model, `model_config` verbatim (continuations copy it; R03.13), prompt hash, user,
  workspace, route, profile and lineage-wide flags (pinned, archived, auto_archived, hidden;
  R03.11), `child_fields` overriding (`id` optional, minted otherwise; `title` optional, written via
  `titles.set_title_in` keeping `title_source`); handoff rows appended (`messages.append_in`) each
  stamped as handoff; then live parent rows with `watermark < id [<= ceiling]` cloned in id order
  (`messages.clone_rows_in`: fresh ids, byte-exact payload, same uids); child counters recomputed.
  Uses the `TRANSCRIPT` budget. Empty `handoff_rows` → `ValueError` before any write.
- Anti-thrash: `counters(store, id) -> CompressionCounters | None`; `record_cooldown(store, id,
  until, error=None)` merge-max (a shorter later deadline never shortens a live one; the error is
  always the latest); `active_cooldown(store, id) -> CompressionCounters | None` (only when
  `until > now`); `clear_cooldown`; `set_counter(store, id, name, value)` for
  `fallback_streak` / `ineffective_count` / `overload_streak` (clamped at 0);
  `bump_overload_streak(store, id) -> int | None` atomic (`UPDATE … RETURNING`).
  `snapshot(store, id) -> CounterSnapshot` and `restore(store, id, snap)` put back exactly what
  was there (cancellation); a restore that cannot be verified raises; a session that vanished
  meanwhile is logged and skipped; restoring "absent" while the row exists raises.
- Chains: `tip(store, id)`, `root(store, id)`, `lineage_ids(store, id)` — thin wrappers over the
  walker in `state.lineage` (slice A; 1,000-step cap per call continued past the cap; more than one
  live continuation → `AmbiguousChain`). `mark_orphans(store, *, older_than=7 days) -> int`: open
  continuations (no end, zero API calls, started before the cutoff, with messages, parent ended by
  compression, kind `continuation`) are ended `orphaned_compression`.

How handoff rows are recognised (asked by the lead): every row `publish_continuation` writes from
`handoff_rows` gets `finish_reason = 'indus.handoff'` (`HANDOFF_FINISH`). A user or summary row
never has a provider finish reason of its own, so this costs no column and survives export/import
and cloning. Listing previews, `recent_user_messages` and rewind's user-turn count skip these rows
(SQL fragment `NOT_HANDOFF_SQL`, Python `is_handoff(row)`). Changing the marker later means changing
the constant and those two helpers only.

Files: `src/indus/state/compression.py`, `src/indus/state/holders.py`, `src/indus/state/handoff.py`,
`tests/unit/state/test_compression.py`, `tests/unit/state/test_holders.py`,
`tests/integration/state/test_compression_concurrency.py`.

Edge cases / failure modes:
- second holder refused; per-session locks independent; 8 threads racing → exactly one wins.
- expired lock reaped; dead PID in our namespace reaped; live PID kept; foreign-namespace,
  other-host and unstamped holders kept until expiry and never probed; probe errors keep the lock.
- lost lock (released and re-taken by another holder, or expired) → nothing published, transcript
  intact.
- 3 rows appended from another thread during a fake slow summary → in the child after the handoff
  rows, in order, original uids, payload columns byte-identical; `ceiling` excludes rows after it.
- the writer may retry the publish callback: the child id is chosen before it runs.
- cooldown merge-max; restore of an exact snapshot (including NULLs).

Our design differences / decisions:
- error classes `CompressionBusy` (`store.compression_busy`), `CompressionClosed`
  (`store.compression_closed`), `CompressionParentEnded` (new code
  `store.compression_parent_ended`).
- the lock is not released by publish (the compressor releases it); the parent keeps its rows.
- no in-place compaction (P08), no turn leases (P17), no recovery deadline column (schema 1 has the
  five counters only), no local-lineage routing.
- holder stamps include the host so a shared volume between machines is never PID-probed.

Tests we will write: lock table above; publish (tail after handoff with uids, every column, lost
lock, expired lock, ceiling, automatic end cleared, deliberate end refused, flags and route
copied, empty handoff); counters (merge-max, clear, streaks, bump, snapshot/restore exact and
vanished); chains (1,200 segments, two live continuations → `AmbiguousChain`); `mark_orphans`;
holders (stamp shape, parse with suffix, staleness matrix with a patched probe). Integration
(`slow`): a writer process appends while this process publishes, 200 iterations, zero lost rows.

## Decisions for the lead
- Handoff marker = `finish_reason = 'indus.handoff'` (above).
- `acquire` on a session that does not exist returns False instead of raising.
- `publish_continuation` takes an optional `ceiling` (the rotator's own pre-publish flush stays out).

Compare checklist (filled after the rewrite; reopened the files named in `Read:`):
- [x] lease claim: delete a stale holder's row, insert-or-ignore, read back, one transaction; 8
  racing threads → one winner (tests: `tests/unit/state/test_compression.py::test_second_holder_is_refused`,
  `::test_expired_lock_is_reaped`, `::test_racing_threads_have_one_winner`)
- [x] refresh by holder only (an expired, unclaimed row can be revived); release only if owned
  (`::test_refresh_and_release_only_for_the_owner`)
- [x] dead-holder rule: own PID never, foreign/unstamped namespace never probed, probe doubt keeps
  (`tests/unit/state/test_holders.py::test_holders_we_cannot_judge_wait_for_expiry`,
  `::test_probe_doubt_keeps_the_row`, `tests/unit/state/test_compression.py::test_dead_pid_in_our_namespace_is_reaped`,
  `::test_foreign_namespace_holder_is_kept_until_expiry`)
- [x] publish fences: lost, expired or never-held lock → nothing written; automatic end cleared,
  deliberate end refused (`::test_lost_lock_publishes_nothing`,
  `::test_expired_or_never_held_lock_publishes_nothing`,
  `::test_automatic_end_stamp_is_cleared_by_the_lock_holder`, `::test_deliberate_or_compression_end_refuses`)
- [x] tail clone after the handoff in id order, byte-exact, same uids; ceiling; child counters
  (`::test_rows_appended_during_the_summary_follow_the_handoff`,
  `::test_tail_clone_is_byte_exact_for_tool_and_multimodal_rows`, `::test_ceiling_keeps_the_rotators_own_flush_out`;
  integration `tests/integration/state/test_compression_concurrency.py`)
- [x] cooldown merge-max, latest error, exact snapshot/restore with the vanished-row rules
  (`::test_cooldown_merge_max_and_latest_error`, `::test_snapshot_restore_is_exact`,
  `::test_restore_tolerates_a_vanished_session_and_refuses_a_new_one`, `::test_restore_that_does_not_stick_raises`)
- [x] orphan marking conditions (`::test_mark_orphans`)
- [x] gap found: an idle-sweep archive (archived + auto_archived) on the parent is lifted when a
  live continuation is published — fixed in `publish_continuation_in`, pinned by
  `::test_idle_sweep_archive_is_lifted_by_a_live_continuation`
- Deliberate differences: `model_config` is copied verbatim to the child (P02-08 / R03.13) instead of
  only conversation-wide keys; `hidden` is carried like the other lineage flags (R03.11);
  `mark_orphans` also requires the child to be a continuation by kind (a branch under a compressed
  parent is never marked); the holder host must match; no PID probe where the namespace is
  unknown (macOS/Windows wait for expiry until P17).
- Not taken: lease refresh inside publish, the recovery-deadline counter (no column in schema 1),
  turn leases (P17), in-place compaction (P08), local-lineage target advance.

## Status

Done (2026-10-18). `compression.py` (33 unit tests), `holders.py` (13 unit tests), shared
`handoff.py` (handoff marker + live-counter recompute, used by listing and rewind), and the `slow`
integration test (200 rotations against a writer process; a local run carried 1,796 concurrent rows,
none lost). New error code `store.compression_parent_ended` in `foundation/errors.py`.
