# Task: In-app installer runner (P20-09) Phase: 20 Owner: wave-34 builder M

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/bootstrap-runner.ts`
(all: event protocol, script resolution, manifest and stage loop, result-frame parsing, run log),
`apps/desktop/electron/first-run-setup-gate.ts` (all), `apps/bootstrap-installer/src-tauri/src/bootstrap.rs`
(stage retry rule only, ~700–800 and 920–945), `apps/bootstrap-installer/src-tauri/src/powershell.rs`
(console decoding and the pipe-drain grace, 1–270). Research 08 §3.5, trap R08.18; P23-03 row (the
contract). Closed before writing.
Evidence: none.

What it does (own words): on a clean machine the desktop installs the runtime itself by driving the
platform installer script (`install.sh`, `install.ps1` on Windows; P23 writes them) through its
stage protocol and showing every step in the boot overlay.

1. The script: a dev env `INDUS_DESKTOP_INSTALL_SCRIPT` may point at a local file; otherwise the
   script for this app's release (`v<appVersion>`) is downloaded fresh from our release origin into
   `<home>/bootstrap-cache/` on every run — an older copy is never reused (a stale script drives
   the wrong tree).
2. `<script> --manifest --non-interactive --indus-home H` → the last stdout line that parses as
   `{protocol_version: 1, stages: [{name, title, category, needs_user_input}]}`; anything else is a
   failure. Emitted as `manifest`.
3. For each stage: `stage {name, running}`, then `<script> --stage NAME --json --non-interactive
   --indus-home H`. Every output line is a `log {stage, line, stream}` event (CR redraws collapse
   to the last frame, ANSI escapes stripped, CRLF and LF both end a line, a line that is not valid
   UTF-8 is decoded as CP1252). The stage result is the **last** stdout line that parses as
   `{ok: boolean, stage: string, skipped?, reason?}` — earlier JSON lines are log text. `ok` and
   `skipped` → `skipped`; `ok` → `succeeded`; otherwise `failed` with the reason.
4. A stage host that exits without a result frame (and was not cancelled) is retried: at most 3
   attempts, 500 ms apart, the wait itself cancellable; a frame saying `ok: false` is never retried.
5. The exit status comes from the process exiting, not from pipe EOF: once the host has exited its
   pipes get a 20 s grace to drain (a grandchild may hold them); after that the rest is abandoned
   and the log says so.
6. Success writes `<home>/.indus-bootstrap-complete` (`{schema: 1, completed_at, app_version,
   script}`, 0600, atomic) and emits `complete {marker}`; the orchestrator then re-runs the
   resolver. Failure emits `failed {stage?, error}`; cancel kills the running host and fails with
   "cancelled".

The renderer sees the events on `indus:installer:event` (`boot.onInstaller`); a 500-line ring keeps
the log for a late window and for the failure overlay; `indus:installer:cancel` cancels. Every stage
host is claimed in the orphan ledger while it runs (P20-13 reaps a host a crashed app left behind).

Inputs → outputs: `InstallerRunner.run({ home, appVersion, platform, repair })` →
`{ ok: true, marker } | { ok: false, stage?, error, cancelled }`; events through `emit`;
`cancel()`; `recentLog()`.

Files: `apps/desktop/electron/installer/runner.ts`, `apps/desktop/electron/installer/stageProtocol.ts`,
`apps/desktop/electron/installer/runner.test.ts`, `apps/desktop/testing/fakeInstaller.mjs`.

Edge cases / failure modes:
- manifest exit non-zero / no JSON / wrong protocol_version → `failed` (no stage runs).
- host death without a frame → retried 3×, then `failed` naming the missing frame.
- a frame with `ok: false` → `failed` at once, no retry.
- grandchild holding stdout → the run completes 20 s after the host exited (fake clock).
- CP1252 bytes (`0x93 0x94`, `0xE9`) decoded; CRLF stripped; a JSON log line before the frame
  does not win.
- cancel during a stage or during the retry wait → `failed: cancelled`, host killed.
- a download that fails (HTTP != 200, network) → `failed` before the manifest.

Our design differences / decisions:
- Script origin: GitHub Releases (D-16.58) asset `install.sh` / `install.ps1` at tag
  `v<appVersion>`; the base is `INDUS_DESKTOP_RELEASE_BASE` (baked default), never a branch.
- Every no-frame exit is retried (the reference retries only the Windows `-1` host exit); bounded
  at 3 like the reference, and only for idempotent stages (the P23 contract makes all stages so).
- No per-run JSONL log file; the events go to `desktop.log` (redacted) and the ring.

Tests we will write (`runner.test.ts`, the fake installer as a real child process):
- full run: manifest → stages → marker written with schema 1 (0600) → `complete`.
- host death without a result → retried 3× then failed; ok:false frame not retried.
- grandchild holding stdout → finishes after the 20 s grace (fake clock), log says truncated.
- CP1252 bytes decoded; last JSON line wins over an earlier JSON line; CRLF + ANSI cleaned.
- cancel kills the host; ring keeps 500 lines; manifest failure.

## Decisions for the lead
- Release base default `https://github.com/indusagi/indus-agent/releases/download` (owner to confirm;
  overridable by the baked env default `INDUS_DESKTOP_RELEASE_BASE`).
- Marker path `<home>/.indus-bootstrap-complete` as the row says (P23-05 also writes
  `<install>/.indus-bootstrap.json`; the resolver trusts neither, it probes).

Compare checklist (filled after the rewrite; reopened the four files in `Read:`):
- [x] event protocol shapes: manifest / stage / log / complete / failed (test: `runner.test.ts::a full run: manifest, every stage, marker with schema 1, complete`)
- [x] gap found: the reference emits an empty manifest before the script download so the windows switch to the install view early — added (same test checks the first event)
- [x] the last JSON line is the frame; an earlier frame-shaped line does not win (test: `::the last JSON line wins over an earlier frame-shaped line`)
- [x] retry only for a missing frame, 3 attempts, 500 ms apart (tests: `::host death without a result frame is retried 3 times, then the run fails`, `::a host that dies once is retried and the run goes on`); ok:false final (test: `::a frame saying ok:false is final (not retried)`)
- [x] the exit status comes from the process exit; pipes drain for 20 s after it (test: `::a grandchild holding stdout: the run finishes once the 20 s grace runs out (fake clock)`)
- [x] CP1252 / CRLF / ANSI / carriage-return redraws (tests: `::console decoding …`, `stage protocol::decoding and line splitting`)
- [x] fresh script every run, never an old copy; pinned to the app's version tag (tests: `::the script is fetched fresh on every run; …`, `installer script source::downloads this release's script fresh into the bootstrap cache`)
- [x] cancel kills the host (test: `::cancel kills the running host and fails the run as cancelled`); manifest failure runs no stage (test: `::a manifest that fails or speaks another protocol runs no stage`)
- Deliberate differences: every no-frame exit is retried (the reference only the Windows `-1` host exit); the script comes from our GitHub release asset at `v<appVersion>` (no branch fallback, no commit pin arguments); no per-run JSONL file (events go to `desktop.log`, redacted); marker written by main with `schema: 1`.
- Not taken: the Windows hand-off to a separate setup app (D-16.47), PowerShell path discovery beyond `powershell.exe` on PATH (P23 owns the Windows script; follow-up when it lands).

## Status

Done (2026-10-21). Fixture `apps/desktop/testing/fakeInstaller.mjs`. Stage hosts are claimed in the
P20-13 orphan ledger while they run. P23-03's real scripts must pass the same frame shapes.
