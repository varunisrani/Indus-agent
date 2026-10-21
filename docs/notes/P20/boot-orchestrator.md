# Task: Boot orchestrator (P20-12) Phase: 20 Owner: wave-34 builder M

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/primary-backend-startup.ts`
(all), `apps/desktop/electron/main.ts` (boot progress and first-run gate 2,447–2,740; `startHermes`
12,032–12,365, read fully), `apps/desktop/electron/first-run-setup-gate.ts` (all). Research 08 §3.2
(boot progress model), §3.5, §3.6; traps R08.1, R08.2. Closed before writing.
Evidence: none.

What it does (own words): one object in main owns "get this window a connection". The first
`connection.get` (or `boot.get`/`boot.retry`) starts an attempt; every window sees the same
`BootProgress { phase, message, progress 0..100, running, error?, retryable?, statusCode?,
latched? }` pushed on `indus:boot:progress` and readable with `boot.get`. Phases and their
progress: resolve 8, remote-connect 24, runtime 28, update-wait, first-run (choice), ready 94,
error. Progress never goes down except when an error (or a fresh attempt after one) allows it.

An attempt: saved remote → remote-connect → ready (a failing remote never starts a local daemon).
Otherwise: wait while an update owns the runtime (update-wait; P20-37 supplies the gate) → resolve
→ if the resolver says "bootstrap needed": first-run choice (continue-local / remote-applied /
reset) → installer (P20-09) → resolve again → attach (P20-10) → ready.

Fencing (R08.1): every attempt has a token; after every await it checks it is still the current
attempt; a superseded attempt's late success publishes nothing and its failure neither publishes
an error nor clears the newer attempt's profile pin. Starting with the same profile while an
attempt runs joins it; another profile supersedes it.

Latching (R08.2): a local failure latches in this process. Two latches: an installer failure (or
cancel) is cleared only by Repair or Reset; any other local start failure is cleared by Retry,
Repair or Reset (and applying a remote). While latched, `start` rethrows the same error silently —
no log line, no installer. Remote failures do not latch except a confirmed reauth rejection (its
own latch, cleared by saving or forgetting the remote, or Reset); transient remote failures are
`retryable: true` so the renderer retries with backoff (P20-16). Repair is allowed at most 3
times per process (soft repairs); it bypasses the usable installed runtime so the installer runs.

Test hooks: `INDUS_DESKTOP_BOOT_FAKE=1` waits `INDUS_DESKTOP_BOOT_FAKE_STEP_MS` (default 650) at each
progress step; `INDUS_DESKTOP_BOOT_FAKE_ERROR=<text>` fails the attempt at resolve with that text
(latched like a local failure).

Inputs → outputs: `BootOrchestrator({ resolve, install, attachLocal, remote, waitForUpdate,
broadcast, sleep, log, hooks })`; `start(profile?)` → `Connection`; `progress()`;
`retry("retry" | "repair" | "reset")` → `{accepted}`; `choose("install-local" | "connect-remote" |
"reset")` → `{accepted}`; `current()`; `onChange(listener)` for `indus:connection:changed`.

Files: `apps/desktop/electron/boot/orchestrator.ts`, `apps/desktop/electron/boot/progress.ts`,
`apps/desktop/electron/boot/orchestrator.test.ts`, `apps/desktop/electron/backend/install.ts`
(wires P20-08..14 into main's registry; new).

Edge cases / failure modes:
- two overlapping starts for different profiles: the older finishing last publishes nothing.
- local failure then Retry from the renderer → no installer run (spy) until Repair.
- reauth (401/403 from the saved remote) latched; Retry does not clear it; save/forget does.
- first-run reset → the waiting attempt ends with a reset error (no latch, progress back to resolve).
- a fourth repair is refused.

Our design differences / decisions:
- Names and phases as in `schemas.ts` (`resolve`, `runtime`, `remote-connect`, `update-wait`,
  `first-run`, `ready`, `error`); no cloud/SSH phases.
- Two local latches (installer vs start) so Retry is useful after an attach failure but never
  re-runs a failed install.

Tests we will write (`orchestrator.test.ts`, all dependencies faked):
- overlapping starts: older finishing last publishes nothing, pin kept.
- local failure + retry → installer spy not called until repair; repair cap 3.
- reauth latched separately; transient remote failure retryable, not latched.
- progress monotone except after an error.
- first-run gate: sentinel + no remote → first-run phase; choices resolve it.
- boot fake hooks.

## Decisions for the lead
- The renderer's `boot.retry({action:"retry"})` clears the start latch, not the installer latch
  (the Verify row: "retry → no installer re-run until Repair").
- Repair cap 3 per process (the 4th answers `accepted: false`).

Compare checklist (filled after the rewrite; reopened the three files in `Read:`):
- [x] attempt fencing: late success publishes nothing, late failure keeps the newer pin (tests: `orchestrator.test.ts::overlapping starts: the older finishing last publishes nothing and keeps the newer pin`, `::an older attempt failing late does not clear the newer pin or publish an error`)
- [x] latching and silent rethrow (test: `::a local failure latches: a retried start rethrows silently without running anything`)
- [x] installer latch survives Retry, cleared by Repair (test: `::an installer failure: Retry never re-runs the installer (spy) until Repair`); repair cap 3 (test: `::repair asks the resolver to skip installed runtimes, and is capped at 3 per process`)
- [x] reauth latched separately; transient remote failures retryable; a failing remote never starts a local daemon (test: `::reauth is latched separately; a transient remote failure is retryable and not latched`)
- [x] first-run gate decisions continue-local / remote-applied / reset (test: `::first-run gate: …`)
- [x] progress monotone except after an error (test: `::progress never decreases except after an error`)
- [x] test hooks (test: `::boot fake hooks: steps wait, a fake error fails at resolve and latches`)
- [x] wired through the real registry with the fake backend (test: `install.test.ts::connection → ticket → dial → REST, with no credential reaching the window`)
- Deliberate differences: two local latches (installer vs start) so Retry helps after an attach failure but never re-runs a failed install; a superseded attempt's callers follow the attempt that replaced it instead of failing.
- Not taken: cloud/SSH latches (host-key change, SSH auth, dead ssh client: N17), the stuck-chooser log timer, the Windows setup hand-off.

## Status

Done (2026-10-21). `electron/backend/install.ts` wires P20-08..14 into the registry; `main.ts`
calls `installBackend(context, electron)` (shared, one call + one guard in the `before-quit`
handler). The update gate is a stub until P20-37.
