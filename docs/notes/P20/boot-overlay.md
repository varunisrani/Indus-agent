# Task: Boot overlay, first-run chooser and failure overlay (P20-16) Phase: 20 Owner: wave-34 builder R

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts` (constants, the bounded boot retry, the
initial-dial loop). Closed before writing.
Evidence: none.

What it does (own words): until main reports `phase: "ready"`, the window shows a boot overlay
fed by `boot.get()` and `boot.onProgress`. States: *connecting* (resolve / runtime / remote-connect
/ update-wait, with the step message and an animated loader), *install* (installer events from
`boot.onInstaller`: the manifest's stage list with each stage's state, and the last log lines),
*first run* (the chooser: "Connect to an existing Indus" or "Install on this computer" →
`boot.choose`), *failure* (message + Retry / Repair / Settings) and *reauth* (the backend refused
our credentials, status 401/403: Sign in again → Settings). A retryable, unlatched failure is
retried by the renderer itself with full-jitter backoff (base 2,000 ms, at most 5 attempts) before
the failure overlay appears; a latched failure (main says retrying cannot help) shows the overlay
at once with Repair offered. Escape never dismisses the install or first-run overlays (they are
modal and keep focus).

Inputs → outputs: `IndusBootProgress`, `IndusInstallerEvent` → overlay view; buttons →
`boot.retry({action})`, `boot.choose({choice})`, `boot.cancelInstall()`, route to `/settings`.

Files: `apps/desktop/src/boot/{BootOverlay.tsx,FirstRunChooser.tsx,bootRetry.ts,bootState.ts}`,
`BootOverlay.test.tsx`, `bootRetry.test.ts`.

Edge cases / failure modes:
- progress events arrive before `boot.get()` answers → the newer one wins (generation counter);
- `boot.retry` refused (`accepted: false`) → the overlay stays, no retry storm;
- the 6th automatic attempt is never made; a manual Retry starts a fresh ladder;
- installer log flood → only the last 200 lines are kept, the last 12 shown;
- a bridge without boot handlers yet (`not-available`) → treated as ready (dev without main).

Our design differences / decisions:
- The retry ladder is a small pure controller (`bootRetry.ts`) with an injectable clock and RNG.
- The renderer decides only the automatic retry; latching stays main's (P20-12).

Tests we will write: each phase renders its view from a fake progress stream; retry delays
within full-jitter bounds with a seeded RNG; no 6th attempt; latched error shows Repair without
waiting; Escape does not close install/first-run; chooser calls `boot.choose`.

## Decisions for the lead
- Reauth is detected from `statusCode` 401/403 on the error snapshot (the progress shape has no
  dedicated flag).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Constants as the reference: 5 attempts, base 2,000 ms, full jitter (tests: `src/boot/bootRetry.test.ts`, seeded RNG).
- [x] Each phase renders; install view with stages and log tail; chooser; reauth; Escape ignored (tests: `src/boot/BootOverlay.test.tsx`).
- [x] No 6th automatic attempt; manual Retry starts a fresh ladder; latched → Repair at once (tests).
- [x] A newer progress event wins over a late `boot.get` answer (test).
- [x] Backend notices (slice M's `indus:backend:notice`): "crashing" shows message, exit code and the last output on the failure view; "restarted" a note while connecting (test).
- Deliberate differences: the reference's renderer-side initial-dial loop (8 × 3 s) is not repeated here: the connection store's ladder takes over after ready.
- Not taken: the HMR survivor adoption path (dev-only in the reference).

## Status

Done (2026-10-21). The notice subscription is looked up as `indusDesktop.backend.onNotice` or
`indusDesktop.boot.onBackendNotice` (whichever slice M's table row names); `BootOverlay` also takes `notices`.

Integration (wave-34 merge): the notice comes from `indusDesktop.boot.onNotice` (`indus:backend:notice`,
P20-13) and `BackendNotice` is the generated `IndusBackendNotice`.
