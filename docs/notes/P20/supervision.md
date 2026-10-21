# Task: Supervision, orphans, quit and power (P20-13) Phase: 20 Owner: wave-34 builder M

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/backend-health.ts`
(constants and readiness wait), `apps/desktop/electron/quit-guard.ts` (all),
`apps/desktop/electron/backend-ownership.ts` (ledger parse, claim, reap), `apps/desktop/electron/main.ts`
(backend reap/stop/ownership 3,787–4,510 skimmed; quit handling 18,329–18,619 skimmed). Research 08
§3.7, traps R08.3, R08.17, R08.24; D-16.81. Closed before writing.
Evidence: none.

What it does (own words):
- **Supervision.** When this app started the profile daemon (no daemon answered `identify` before
  ensure), main watches it by asking `identify` every 2,000 ms. If it stops answering after ready,
  main ensures it again (which restarts it), waits for readiness (up to 180,000 ms, a probe every
  500 ms, each probe ≤ 5,000 ms) and tells every window "stopped working in the background" on
  `indus:backend:notice`. After 3 restarts within 120,000 ms it stops respawning and sends
  "keeps crashing" with the exit code (when known) and the last lines of the daemon's stdio log
  (redacted). A daemon this app only attached to is never restarted by it.
- **Orphans.** Processes the app itself owns (installer stage hosts while they run) are recorded in
  `<userData>/backend-ownership.json` with pid, process start time and a nonce. On start, the lock
  holder (guarded by `machinery`) reaps an entry only when the pid is alive **and** its start time
  still matches; otherwise the entry is dropped and nothing is killed (a reused pid belongs to
  someone else). A corrupt ledger is moved aside, nothing killed.
- **Quit.** Local and remote daemons outlive the app, so when windows report active turns
  (`indus:work:active`) the quit prompt only informs ("the agent keeps working; reconnect later")
  and never kills anything; app-owned work (an install in progress) asks to confirm, listing up to
  4 items and "N more".
- **GPU.** A GPU child that dies with a known crash signature gets one bounded relaunch with the
  GPU disabled (per app version, a marker in userData); the renderer sandbox is never disabled
  (D-16.81).
- **Power.** A power-resume event triggers remote revalidation through P20-11's 15,000 ms hold.

Inputs → outputs: `DaemonSupervisor.watch({ startedByApp })`, notices through `notify`;
`OrphanLedger.claim/release/reapOrphans()`; `QuitGuard.report(senderId, items)`,
`QuitGuard.prompt(ownership)` → `null | {message, detail, buttons, confirm}`; `gpuCrashDecision`.

Files: `apps/desktop/electron/backend/supervisor.ts`, `apps/desktop/electron/backend/orphans.ts`,
`apps/desktop/electron/quitGuard.ts`, `apps/desktop/electron/backend/supervisor.test.ts`,
`apps/desktop/electron/backend/orphans.test.ts`, `apps/desktop/electron/quitGuard.test.ts`.

Edge cases / failure modes:
- 4th crash in 120 s → no respawn (fake clock); crashes spread wider than 120 s keep respawning.
- restart that never becomes ready within 180 s → "keeps crashing" with the reason.
- pid reused by another process (start time differs) → not killed, entry dropped.
- reap on a lock loser → refused by `machinery.guard`.
- quit with an attached local daemon and running turns → informational prompt, no kill.
- a window that closes stops counting in the work report.

Our design differences / decisions:
- Liveness of a detached daemon by control-socket `identify` polling (it is not our child, so
  there is no exit event); the exit code is read from the daemon's status record when present.
- The ledger holds only app-owned children (installer hosts in 1.0); daemons and serves are never
  in it (they are meant to outlive the app).
- New event channel `indus:backend:notice` (`boot.onNotice`).

Tests we will write:
- `supervisor.test.ts` (fake clock, fake ensure/identify): crash → respawn + notice; 4th crash in
  120 s → no respawn, "keeps crashing" with code and output; attached-only daemon never restarted;
  readiness timeout.
- `orphans.test.ts`: matching pid + start time killed; reused pid not killed; corrupt ledger moved
  aside; loser refused.
- `quitGuard.test.ts`: attached → informational, no kill; app-owned → confirm with ≤ 4 items;
  merge across windows; GPU decision table.

## Decisions for the lead
- Watch interval 2,000 ms (not in the row).
- New event channel `indus:backend:notice` owned by P20-13; the renderer (P20-16) shows it.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] 3 restarts within 2 minutes, then "keeps crashing" with exit code and output (tests: `supervisor.test.ts::4th crash in 120 s → no respawn; …`, `::crashes spread wider than two minutes keep being restarted`)
- [x] readiness 180,000 / 500 / 5,000 (tests: `::readiness wait: polls every 500 ms and gives up at 180 s`, `::withTimeout bounds a probe`)
- [x] attached-only daemon never restarted (test: `::a daemon this app only attached to is never restarted`)
- [x] pid + start time identity, never an unrelated process (tests: `orphans.test.ts::a reused pid (start time differs) is not killed, and the entry is dropped`, `::a real child: identity read from the OS, reaped on the next start`); corrupt ledger moved aside (test: `::a corrupt ledger is moved aside and nothing is killed`); lock loser refused (test: `::a lock loser may neither claim nor reap`)
- [x] quit prompt by ownership, 4 listed (tests: `quitGuard.test.ts::attached local daemon with running turns → informational prompt, nothing is stopped`, `::app-owned work asks to confirm, …`; wired: `install.test.ts::quit with an attached daemon …`)
- [x] GPU relaunch once per version, sandbox untouched (tests: `quitGuard.test.ts::gpu crash relaunch`)
- Deliberate differences: liveness by control-socket `identify` polling (the daemon is not our child); the ledger holds only app-owned children (installer hosts); the GPU relaunch keeps every switch the person passed and adds only `--disable-gpu` (D-16.81).
- Not taken: window-close guard on Windows/Linux when the last chat window closes with work (not in the row), SSH-owned backends (N17), per-version crash-loop sandbox markers (D-16.81).

## Status

Done (2026-10-21). New event channel `indus:backend:notice` (`boot.onNotice`) for P20-16 to show.
`INDUS_DESKTOP_SKIP_QUIT_PROMPT=1` skips the quit prompt (P20-40's E2E).

Integration (wave 35): the "restarted" notice arrives after the daemon is ready again, when the boot overlay is gone; it now shows once as a toast over the recovered window (15 s, closable; D-16.720, `BootOverlay.test.tsx::a restart reported after recovery shows once as a toast over the ready window, then goes`).
