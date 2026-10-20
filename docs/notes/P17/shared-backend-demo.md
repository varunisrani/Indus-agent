# Task: Shared-backend demo suite (P17-27) Phase: 17 Owner: wave-28 builder L3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — the row's reference column is
"—": the demo is our own acceptance flow (M6, CLI + TUI half). Ours read: the P17 phase file "Done
when" demo, `tests/e2e/test_demo1.py` and `tests/e2e/conftest.py` (the REPL in a pseudo-terminal),
`apps/tui/src/cards/inkHarness.ts`, `apps/tui/src/app/App.test.tsx`, `apps/tui/src/entry.tsx`
(how the renderer attaches by default), `src/indus/cli/attach.py` (what the attached REPL says about
lost turns), `src/indus/service/remote/host.py`.
Evidence: none.

What it does (own words): one end-to-end run of the shared backend with real processes. A daemon
(`indus gateway run`) serves a temp profile whose model is the scripted fake on loopback. The
classic CLI (the real REPL in a pseudo-terminal) and the TUI (the real `App` in Ink's test renderer,
over the real runtime, client and attach transport, driven over stdin/stdout) attach to one session.
A prompt typed in the CLI streams in both; the approval it raises is answered in the TUI and the
CLI's card closes ("question answered in another window"); the CLI's terminal closes while a turn
runs and the turn still completes (the TUI shows it, the ledger says completed); the daemon is
killed and started again while a TUI turn runs — both clients say its outcome is unknown, the CLI
lists it with its `/discard` line, the ledger row is `unknown`, and the model is never asked again;
finally, with the daemon stopped, `indus --local -z` answers in process and starts no daemon.

Inputs → outputs: the fake model's script (6 turns) → CLI screen text, TUI state/frames, ledger
rows, model request count, daemon presence.

Files: `tests/e2e/service/test_shared_backend.py`, `tests/e2e/service/__init__.py`,
`apps/tui/src/test/sharedBackendDriver.tsx` (test-only driver, built with `vite build --ssr` into
`apps/tui/node_modules/.shared-backend/` by the test). Shared (fixes the demo needed):
`src/indus/service/remote/host.py` + `tests/unit/service/remote/test_host.py`,
`src/indus/cli/attach.py`, `src/indus/cli/attach_remote.py`; the P17-26 fixes (daemon turn-end
events, session source, TUI attach wording) are listed in `dual-host-matrix.md`.

Edge cases / failure modes:
- the TUI's fresh session has no stored row until its first turn (D-16.426), so the CLI cannot
  select it; the demo opens the session with one attached `chat -q` first and both resume it;
- after the kill, either the test or a client's own recovery (`ensure` recover budget) may start the
  next daemon; the test waits for whichever daemon answers with a new instance id;
- the CLI renders another surface's streamed text at line ends, so the CLI's sight of the TUI's
  stalled turn is its turn header (`◇ indus`);
- every process is stopped at the end; none naming the temp home is left (checked).

Our design differences / decisions:
- "Ink test renderer": the driver mounts the app with the TUI's own `inkHarness.mount` (fake stdin/
  stdout, frames ANSI-stripped) instead of a pseudo-terminal, so the test reads the stores and the
  frame text directly; the link, runtime and components are the shipping ones.
- The driver is built by the test (3 s) rather than shipped: it is test-only.

Tests we will write: `test_shared_backend.py::test_cli_and_tui_share_one_session_on_one_daemon`
(every step of the row).

## What the demo found, and what was done

- **The attached CLI's approval card stayed open after the TUI answered** (until its 300 s
  deadline). `RemoteSessionService` dropped the offer on `request.settled` but told nobody; it now
  passes an `answered` settlement on as `server_request.cancelled {answered_elsewhere}`, which the
  REPL already closes with "question answered in another window" (`test_host.py::
  test_an_approval_answered_by_another_viewer_closes_here`, fails without the change).
- **After a daemon restart the attached CLI REPL said nothing** about the lost turn (the lost-turn
  list was read only at start). The REPL now listens for the session's replay gap (a reconnect to a
  restarted daemon) and lists the lost turns again with their `/discard` lines
  (`attach._report_lost_turns`; `AttachedHost.on_gap`, `RemoteAttached.on_gap`; pinned by the e2e).
- **The TUI said "The agent process stopped; restarting…" and nothing about the lost reply**: see
  `dual-host-matrix.md` (attach wording and the carried outcome-unknown line).

## Decisions for the lead
- The TUI does not show the prompt another surface typed (no event carries a person's message to
  other viewers); it shows the reply. Proposed: the daemon emits a `message.user`-style event (or
  `turn.started` carries the input preview) — P17-25 protocol.
- After a reconnect the attached TUI's transcript is empty: the daemon's `session.resume` carries no
  stored messages (the bridge's does). Proposed (P17-25 / follow-up): `session.resume` over the
  daemon returns the transcript rows the bridge returns, so the reopen shows the conversation.
- The TUI cannot list or discard a lost (`unknown`) admission; it only says its own running reply's
  outcome is unknown. Proposed: `admission.state` events (P17-25) and an `/discard` in the TUI.

Compare checklist (filled after writing; no reference file — compared against the row's steps):
- [x] daemon started; CLI and TUI attached to one session
- [x] a prompt from the CLI streams in both
- [x] an approval raised during it is answered from the TUI and the CLI card closes
- [x] the CLI detached mid-turn; the turn completes
- [x] daemon restarted mid-turn; both report the outcome as unknown and neither resends
- [x] `--local` works with the daemon stopped (and starts none)
- Deliberate differences: the session is opened by one finite attached run first (above).
- Not taken: the dashboard half of M6 (P18).

## Status

Done (2026-10-20). The e2e passed 3 runs of 3 (~30 s each).

Integration (2026-10-20): D-16.551 (attached surfaces after a lost link), D-16.552 (the demo).
`admission.state` is now emitted by the daemon (D-16.553); the TUI does not list or discard
lost admissions yet (follow-up). The demo file also holds the P17-23 check (an auto-started
daemon idles out, the next `indus chat -q` restarts it silently, D-16.557).
