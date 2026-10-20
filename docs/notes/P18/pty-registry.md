# Task: PTY keep-alive registry (P18-08) Phase: 18 Owner: wave-29 builder W2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/pty_session.py` (all:
ring buffer, session attach/detach/write, registry, reaper), `hermes_cli/web_server_chat.py`
(registry limits, per-channel active-session file), `hermes_cli/web_routers/chat_ws.py`
(`/api/pty` keep-alive branch: key, close-others, orphans, created/marker ownership, writer loop).
Research 09 §3.5 (server flow 2, 3, 5, 6, 8; registry limits), traps R09.6, R09.8. Our P17
`state/turn_leases.py`, `state/browse.py`, `service/turn_lease.py`. Closed before writing.
Evidence: none.

What it does (own words): a browser tab switch, a page reload or a proxy dropping an idle socket
must not end the chat. With an `attach` token the PTY outlives its socket: one drain task per PTY
reads the child into a 1 MiB ring buffer and forwards to whichever socket is attached. A later
socket with the same key (attach token + profile + the resume id the client asked for) gets the
ring replayed and then one Ctrl-L byte so the TUI repaints a whole frame (a byte tail of an
alternate-screen program cannot be rebuilt). The older socket of that key is closed 4409
("superseded", the client stays quiet); when the child exits the attached socket is closed 4410
("start a new session"). A disconnect only detaches. A reaper ends PTYs detached for 30 minutes,
or whose child is gone; at most 16 PTYs exist — a new one first evicts the longest-detached one,
and when every slot has a viewer the request fails with an explanation and 1011. Input from a
socket that is not the attached one is dropped; input the child does not take within 10 s closes
the socket 1013 and marks the PTY dead.

Before attaching, other PTYs of the same raw token (the tab changed profile or resume target) are
closed — a viewer still on one is told 4409 — and so are detached PTYs that hold the conversation
being resumed: those with the same profile and resume id, or whose process tree contains the
turn-lease holder of that session in that profile's store (P17). After P17 the TUI is a viewer
of the daemon, so ending a PTY (reaper, eviction, orphan) ends only the TUI: the daemon keeps the
turn.

Without `attach` the socket and the PTY live and die together (1:1): spawn, pump both ways, end
the child on disconnect; the channel marker this socket made is dropped in a `finally` that a
cancelled handler still runs.

Channels: a `channel` (`[A-Za-z0-9._-]{1,128}`) pairs a PTY with its event feed. Per channel the
backend keeps an active-session file the TUI writes its live session id into; a later socket on
the channel without `resume` reopens that session (and first sends `{"type": "resume", "id"}` as
a JSON text frame so the client treats it like an explicit resume); `fresh` deletes it first. The
file belongs to the PTY that was created with it and is removed when that PTY ends; a marker made
by a socket that then attached to an existing PTY is dropped at once. A picked working directory
(`cwd`) applies to fresh chats only and must be an absolute path to an existing directory —
anything else fails closed (never the dashboard's own launch directory).

Inputs → outputs: `PtyRegistry.attach_or_spawn(key, spawn, on_close=)` → `(PtySession, created)`;
`close_others(token, keep=)`, `close_orphans(key, holder_pid=)`, `detach(key, viewer)`,
`reap(now=)`, `run_reaper()`, `close_all()`; `PtySession.attach(viewer, redraw=)`, `.write(viewer,
data)`, `.detach(viewer)`. `ChannelFiles.claim/read/clear/release`; `resolve_cwd(raw)`.

Files: `src/indus/web/pty/registry.py`, `src/indus/web/pty/channels.py`; tests
`tests/unit/web/pty/test_registry.py`, `tests/unit/web/pty/test_channels.py`.

Edge cases / failure modes:
- two sockets racing one key → one registry lock around get-or-spawn; one PTY per key.
- a socket superseded while its replay was being sent; a client gone mid-replay (detach, so the
  reaper can reclaim it); a superseded socket's late input or late detach (ignored).
- a replacement socket attaching while the old one's write waits on back-pressure: a late failure
  of the old write must not mark the shared PTY dead.
- the PTY dies between reaps → removed on the next attach or reap; a dead remnant under the key
  is closed in the background (its helpers' grace must not hold the registry lock).
- overlapping reaps (attach path and reaper) → a key popped once.
- channel file: created 0600 with `O_EXCL | O_NOFOLLOW` in a 0700 directory of our own; an
  unreadable or malformed file reads as "no session".

Our design differences / decisions:
- Keys are a small frozen dataclass (`PtyKey(token, profile, resume)`), not a NUL-joined string.
- Descendant check through `/proc/<pid>/stat` parents (Linux; elsewhere only the leader pid
  matches) instead of psutil.
- Times come from an injected `Clock` (reaper interval, TTL) so tests drive them.
- The lease holder is read browse-only (`state.browse.open_readonly`), never opening or migrating
  the store; an unreadable store means "no holder".

Tests we will write: fake bridge + fake clock: second attach → first closed 4409, ring replayed
then `\x0c`; ring keeps the last 1 MiB; child exit → 4410; 17th session → `RegistryFull` (16
viewers) and eviction of the oldest detached one otherwise; reaper at 30 min (not at 29); input
from a superseded socket dropped; stalled write → dead; close-others / orphans (same resume, lease
holder in the tree, attached ones kept); daemon turn survives reaping (fake daemon asserts no
interrupt). Channels: id pattern, claim/read/clear/release, permissions, malformed file, cwd rules.

## Decisions for the lead
- "Canonical resume id" = the id the client asked for, as given (no compression-lineage walk on
  the server: the TUI resolves the lineage itself when it resumes). A follow-up can canonicalise
  through the store once a browse-only lineage read exists.
- The reaper interval is 60 s (as the reference); a dead child is also noticed on every attach.

Compare checklist (filled after the rewrite; reopened `hermes_cli/pty_session.py`,
`hermes_cli/web_routers/chat_ws.py`):
- [x] ring of the last 1 MiB (test: `tests/unit/web/pty/test_registry.py::test_ring_keeps_the_last_bytes`)
- [x] reattach: older socket 4409, ring replayed, then Ctrl-L for a reused PTY only
  (test: `test_registry.py::test_second_attach_supersedes_and_replays_then_redraws`,
  `tests/contract/web/test_pty_route.py::test_reattach_supersedes_replays_and_redraws`)
- [x] client gone mid-replay → detached so the reaper can reclaim it
  (test: `test_registry.py::test_a_viewer_gone_mid_replay_is_detached`)
- [x] superseded socket's input and late detach ignored; late write failure of a superseded socket
  does not kill the shared PTY (generation check) (test: `test_registry.py::test_second_attach_…`)
- [x] child exit → 4410 (test: `test_registry.py::test_child_exit_closes_the_viewer_4410`)
- [x] stalled input → dead PTY, socket 1013 (test: `test_registry.py::test_stalled_input_marks_the_pty_dead`,
  `test_pty_route.py::test_stalled_input_closes_1013`)
- [x] cap 16: evict the longest-detached, else `RegistryFull` → text + 1011
  (test: `test_registry.py::test_seventeenth_pty_evicts_the_oldest_detached`,
  `test_pty_route.py::test_a_full_registry_explains_and_closes_1011`)
- [x] TTL 30 min after detach; dead children reaped; reaper every 60 s
  (test: `test_registry.py::test_reaper_ends_a_pty_thirty_minutes_after_detach`,
  `test_registry.py::test_run_reaper_ticks_on_the_clock`)
- [x] one PTY per key under racing attaches (test: `test_registry.py::test_one_pty_per_key_under_racing_attaches`)
- [x] close-others by raw token with 4409 to a viewer; orphans by profile + resume or lease holder
  in the process tree, never one with a viewer, only when resuming
  (test: `test_registry.py::test_close_others_ends_the_token_s_other_ptys`,
  `test_registry.py::test_orphans_holding_the_resumed_chat_are_ended`,
  `test_pty_route.py::test_orphans_holding_the_resumed_chat_are_ended`)
- [x] reaping ends the TUI only; the daemon's turn keeps running
  (test: `test_registry.py::test_reaping_a_tui_leaves_the_daemon_turn_running`)
- [x] channel files: implicit resume announced, fresh drops it, marker ownership (created PTY owns
  it; a marker made for an existing PTY is dropped; 1:1 drops its own in a `finally`)
  (test: `tests/unit/web/pty/test_channels.py::test_claim_read_clear_release`,
  `test_pty_route.py::test_implicit_resume_is_announced_explicit_is_not`,
  `test_pty_route.py::test_one_to_one_ends_the_child_and_drops_its_marker`)
- [x] cwd for fresh chats only, absolute + existing, else fail closed
  (test: `test_channels.py::test_cwd_rules`, `test_pty_route.py::test_failures_show_red_text_and_close_1011`)
- Deliberate differences: TTL compared with `>=` (a PTY detached exactly 30 min is reaped); keys
  are a dataclass with the canonical profile id (the reference keys on the raw `profile` text);
  descendants read from `/proc` (no psutil); channel files live in one private directory of ours,
  not loose temp files.
- Not taken: resume canonicalisation through the compression lineage on the server (see
  decisions).

## Status

Done (2026-10-20). Shared: none.
