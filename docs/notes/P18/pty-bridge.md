# Task: PTY bridge (P18-07) Phase: 18 Owner: wave-29 builder W2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/pty_bridge.py` (all:
spawn, read/write, resize clamp, close cadence), `hermes_cli/web_server_chat.py` (the resize
pattern, the 1:1 pump, spawn argv/env resolution). Research 09 §3.5 (server flow 1, 7) and traps
R09.6, R09.7, R09.19. Our `safety/child_env.py` (R02.22), `cli/tui_launch.py` (P12-32). Closed
before writing.
Evidence: none.

What it does (own words): the dashboard's Chat tab runs the real TUI behind a pseudo-terminal and
streams its bytes to xterm in the browser. The bridge owns one child process on one PTY: it spawns
the child (argv, cwd, env given by the caller — the env comes from the single child-env builder),
reads what the child prints, writes what the person types, applies terminal size changes and ends
the child and its helpers. Size changes travel in-band: a socket frame that is exactly
`ESC [ RESIZE:<cols>;<rows> ]` is a resize, consumed by the server, applied with `TIOCSWINSZ` and
never written to the child; the numbers are clamped to 1..2000 columns and 1..1000 rows (a broken
probe reporting 131072 columns or 0 rows would otherwise blank the TUI or crash the pack). Without
the bridge the chat tab has nothing to show.

Inputs → outputs: `spawn(argv, cwd=, env=, size=)` → a bridge with `pid`, `read()` (bytes; None at
EOF), `write(data)` (False when the child stopped accepting input for 10 s or the PTY is gone),
`resize(size)`, `is_alive()`, `close()`. `resize.parse_resize(frame)` → `TermSize | None`.
`bridge.unavailable()` → None, or the reason a PTY cannot be made on this machine.

Files: `src/indus/web/pty/__init__.py`, `bridge.py`, `resize.py`; tests
`tests/unit/web/pty/test_resize.py`, `tests/integration/web/test_pty_bridge.py`; fake child
`tests/fakes/pty/fake_tui.py`.

Edge cases / failure modes:
- resize frame with extra bytes before/after, or two escapes in one frame → plain input (written);
  zero, negative-looking or huge numbers → clamped; more than 9 digits → not a resize.
- child exits while a helper still holds the slave side open → no EOF ever; the read loop asks
  the leader (WNOHANG) on every 0.2 s read timeout and reports EOF once it is gone and the master
  has nothing left to read.
- the child stops reading its input (full PTY buffer) → `write` waits on writability without
  blocking the loop, gives up after 10 s → the route closes the socket 1013.
- close: SIGHUP to the child's process group (it is a session leader), wait up to the grace
  (1.5 s, capped at 2.0 s) while draining output, then SIGTERM (0.5 s), then SIGKILL (0.5 s); the
  leader is reaped (no zombie) and the group swept with SIGKILL at the end so a helper that ignores
  SIGHUP cannot keep the PTY. Idempotent.
- Windows: ConPTY through `pywinpty` when it imports; when it cannot, `unavailable()` names the
  reason and the route writes one red "chat needs WSL2 on this machine" line and closes 1011
  (D-16.38). POSIX without `ptyprocess` → a plain "install the PTY support" reason, same close.
- fd reuse: the reader's readiness watch is removed by `close()` before the fd is closed, and a
  late wake-up of a closed bridge never touches a newer bridge's fd.

Our design differences / decisions:
- Reads are readiness-driven on POSIX (`loop.add_reader` with a 0.2 s timeout per wait) instead
  of a worker thread per PTY polling `select` with a 0.05 s idle back-off: no thread per terminal,
  no idle wake-ups; the 0.2 s timeout is kept for the liveness check. The Windows path reads on
  one dedicated thread per bridge and backs off 0.05 s when idle.
- The PTY child is `python -m indus --tui` for the scope's profile (so its argv and env are exactly
  what `indus --tui` builds, P12-32), not a second copy of the launcher's resolution; argv/env are
  built by `ws/pty_route.py` (P18-09); the bridge only runs what it is given.
- No psutil: the group sweep uses `killpg`; descendant checks (P18-08) read `/proc`.

Tests we will write:
- resize: exact frame parsed; clamps (0, 5000×5000, 1×1); prefix/suffix/double frames not
  resizes; text and bytes messages turned into bytes.
- bridge (real PTY, fake TUI child echoing raw input): a resize frame never reaches the child and
  the child sees the new size; typed bytes echoed; EOF after the child exits; `write` false after
  close; close kills a SIGHUP-ignoring helper in the group; no zombie left.
- Windows path: import failure (monkeypatched loader) → `PtyUnavailable` with the WSL2 text.

## Decisions for the lead
- POSIX reads use readiness callbacks (above) — the row's "0.2 s read timeout and 0.05 s idle
  back-off" holds for the Windows thread reader; on POSIX only the 0.2 s liveness tick remains.
- `ptyprocess` (ISC, already locked as the `pty` extra) is what the bridge imports; P18-01's
  `web` extra should list it (and `pywinpty` on Windows). Tests needing it skip without the extra.
- No psutil dependency for the bridge (killpg + /proc instead).

Compare checklist (filled after the rewrite; reopened `hermes_cli/pty_bridge.py`,
`hermes_cli/web_server_chat.py`):
- [x] resize escape consumed only when the frame is exactly the escape
  (test: `tests/unit/web/pty/test_resize.py::test_only_an_exact_frame_is_a_resize`,
  `tests/contract/web/test_pty_route.py::test_a_real_child_never_sees_the_resize_escape`)
- [x] clamp 1..2000 / 1..1000, garbage → 1 (test: `test_resize.py::test_sizes_are_clamped`,
  `test_resize.py::test_clamp_turns_garbage_into_one`)
- [x] TERM backfilled when missing, an explicit one kept
  (test: `tests/integration/web/test_pty_bridge.py::test_spawn_backfills_term_and_keeps_the_given_env`)
- [x] a host marker for the TUI (`INDUS_PTY_HOST=dashboard`), set by the route's child env
  (test: `tests/contract/web/test_pty_route.py::test_child_env_comes_from_the_builder`)
- [x] EOF on EIO/EBADF; EAGAIN is "nothing yet"; EOF once the leader is gone even if a helper
  holds the slave (test: `test_pty_bridge.py::test_eof_when_the_child_exits_and_no_zombie`)
- [x] non-blocking writes wait on writability, give up after the timeout
  (test: `test_pty_bridge.py::test_a_child_that_stops_reading_stalls_the_write`)
- [x] close: hang-up first with a grace capped at 2 s, then terminate, then kill; group swept; leader
  reaped (test: `test_pty_bridge.py::test_close_ends_a_helper_that_ignores_hangup`,
  `test_pty_bridge.py::test_a_hangup_aware_child_ends_inside_the_grace`)
- [x] Windows without the ConPTY binding: one red WSL2 explanation, close 1011
  (test: `test_pty_bridge.py::test_windows_without_conpty_explains_wsl2`,
  `test_pty_route.py::test_failures_show_red_text_and_close_1011`)
- [x] gap found: the reference turns mouse tracking off for the dashboard child (the browser wheel
  must scroll) — fixed by `INDUS_TUI_MOUSE=off` in the route's child env, pinned by
  `test_pty_route.py::test_child_env_comes_from_the_builder`
- Deliberate differences: the reference reads on executor threads with select + idle back-off; we
  use readiness callbacks on POSIX (no thread per terminal). It snapshots descendants with psutil
  when the child shares the server's process group; our child is always its own session leader,
  so one group signal reaches every helper. Its grace follows an env value of the TUI gateway; ours
  is a constructor value (1.5 s, capped at 2.0 s). The PTY child is our real `indus --tui` entry
  instead of a re-resolution of the renderer's argv and env.
- Not taken: inline-mode switch for the TUI (ours has no such mode); the PM-install error path
  (no package manager in Indus).

Integration (P18 integrator, 2026-10-20): the extra is `dashboard` (ptyprocess 0.7.0, pywinpty
3.0.5 on Windows; D-16.585) and the missing-package notice names `indus-agent[dashboard]`.

## Status

Done (2026-10-20). Shared: none for this task (the launcher line is P18-09's). `ptyprocess`
spawns with `forkpty` from a multi-threaded process (Python warns); the same is true of the
reference — a follow-up could spawn through `posix_spawn` + `TIOCSCTTY` in a tiny exec helper.
