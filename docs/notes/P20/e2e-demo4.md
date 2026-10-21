# Task: Electron E2E and Demo 4 core suite (P20-40) Phase: 20 Owner: wave-35 integrator

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/e2e/core/README.md` (all
105 lines: the three issue classes, the oracle's transient checks, lifecycle census, packaged smoke,
suite rules) and the titles of `apps/desktop/e2e/` and `apps/desktop/e2e/core/` (file names only) —
both opened at the Compare step, after the suite was written from the phase row, research 08 and our
own P20-18 oracle (`testing/oracle.ts`, `docs/notes/P20/transcript.md`).
Evidence: none.

What it does (own words): the whole desktop app — the built renderer and main process in real
Electron 44.5.1 windows — is driven end to end against a fake backend and against a real `indus`
daemon and serve, with only the model scripted. It proves Demo 4's path on a clean home: install (the
stage-protocol fake installer), boot, attach, onboarding, a streamed chat with a tool row and an
approval answered once, the working panes, recovery from a killed daemon without a duplicated
message, and that hostile content can never open the OS browser. It is the lane CI runs after the
unit tests (`desktop-e2e`), plus the packaged smoke in `desktop-package`.

Inputs → outputs: `pnpm --filter @indus/desktop e2e [spec]` (vitest, `apps/desktop/e2e/vitest.config.ts`)
→ per spec: a temp app build outside the checkout, a temp home / userData per launch, an xvfb display
(`xvfb-run -a`), a DevTools session per window (`testing/electron/cdp.ts`); the real-runtime specs
spawn `tests/fakes/web_stack.py` (scripted model on loopback, a real profile home, a daemon or none);
assertions on the DOM, on the wire (fake daemon's recorded frames, fake serve's recorded REST
calls), on the store (`state.db` via the stack's `sql`), on processes (`/proc` by `INDUS_HOME`) and
on an `xdg-open` recorder placed first on the app's PATH.

Files: `apps/desktop/e2e/{fixtures.ts, vitest.config.ts}`, `apps/desktop/e2e/core/{boot,chat,
lifecycle,security,surfaces,packaged}.electron.test.ts` (the packaged smoke moved here from
`testing/electron/packaged.electron.test.ts` and extended), `apps/desktop/testing/fakeTrayWatcher.py`,
`testing/fakeBackend/server.mjs` (records REST calls), `testing/electron/cdp.ts` (bounded calls),
`apps/desktop/package.json` (`e2e` script), `.github/workflows/ci.yml` (`desktop-e2e`, packaged smoke).

Edge cases / failure modes:
- a DevTools call to a page between documents never answers: every call is bounded (30 s; 5 s per
  poll in `waitFor`), a poll that times out is retried until the step's own deadline;
- the app's first REST calls race the boot: lists and history are asserted after "connected", and
  the product now reloads them when the connection becomes ready (D-16.718, D-16.724);
- a single final-state oracle misses transient duplicates: the restart spec samples every frame
  (40 ms) from `kill -9` to recovery and runs `checkFrames` (it caught D-16.719);
- the single-instance lock: each launch has its own userData; deep links use a second launch on the
  same userData (the OS path);
- the OS browser: never touched — a recorder; a trusted click is the positive control proving it
  records;
- no leftovers: every app is stopped by its process group, the stack reports `stray == []`, and the
  lifecycle spec asserts no process names the home after the stack stops.

Our design differences / decisions:
- Our DevTools driver under vitest instead of Playwright `_electron` (D-16.726): the driver already
  proved the wired app (wave 34), needs no `--inspect` (a fused packaged build refuses it) and adds no
  dependency to the desktop.
- Linux only for now: the recorder stands in for the OS browser via `xdg-open`, the fake daemon's
  control socket is POSIX, the tray host is a private session bus + `fakeTrayWatcher.py`; macOS and
  Windows lanes are follow-ups. 1 retry on CI (R10.20), reported in the vitest summary.
- One app per real-runtime spec file, tests in order (a Demo walks forward); the fake-backend specs
  start fresh apps where state would leak.

Tests we will write:
- boot: healthy boot (gui viewer, list loaded, no onboarding with a provider), latched failure
  overlay, first run (chooser → installer stages → managed runtime found → connected);
- chat (real runtime): no overlay for a keyless custom endpoint; onboarding re-run from Settings
  through the real `/api/model/test-endpoint`; deep link → Settings → default folder; settings search
  "proxy" over the real schema; streamed reply, write_file tool row, approval card answered once;
  git review / files / preview panes; transcript-once after reload; second chat and back via the
  sidebar; profile rail switch to a new profile (own daemon) and back;
- lifecycle (real runtime, app starts the daemon): `kill -9` → one notice, every frame
  duplicate-free, next prompt answered; quit; three relaunches attach; nothing left;
- security: four hostile guests in the real preview, trusted click positive control, fs confinement
  through symlinks, terminal env floor, deep-link refusals/confirmations, media roots, link previews
  refuse private hosts, diagnostics redacted, CSP refuses an injected inline script;
- surfaces: HUD pop-out from the sidebar, global shortcut registration and refusal, tray without and
  with a StatusNotifier host (hide on close, back when the host leaves), update check against a
  loopback feed, first-run onboarding by API key (backend only, nothing in renderer storage);
- packaged: `--dir` build (or CI's), fuses verified, archive checked, packaged app interactive
  ≤ 60 s with CSP enforced.

## Decisions for the lead
- Product fixes the suite forced are recorded as D-16.716 – D-16.725 and D-16.727 (history over REST, transcript
  merge, restarted toast, keyless endpoint, lists after boot, settings closes before onboarding,
  tray toggle semantics, one device-settings store, fs roots from the shell, packaging wiring, one
  daemon ensure at a time).
- `desktop-e2e` joins the gate's `needs` (it has never run in CI: the job installs xvfb, dbus and
  python3-gi and downloads Electron with `install.js`).

Compare checklist (filled after the rewrite; reopened `apps/desktop/e2e/core/README.md` and the
`e2e/` titles):
- [x] Transcript-once oracle: persisted messages exactly once and in order after reload and after a
  daemon restart (`chat.electron.test.ts::transcript-once …`, `lifecycle.electron.test.ts::kill -9 …`).
- [x] Transient duplicates sampled, not only the final DOM (`lifecycle…::kill -9 …` runs
  `checkFrames` over ~40 ms frames) — gap found and fixed (D-16.719), pinned by
  `src/chat/messageStream.test.ts::a finished reply whose user row is bound elsewhere is not shown twice by the snapshot`.
- [x] Approval answered once (`chat…::chat: streamed reply …`; fake backend: the wired test counts
  one `server_request.answer` on the wire).
- [x] `kill -9` → exactly one supervised respawn and a working turn; three relaunches of the same
  home; nothing left after (`lifecycle…`).
- [x] Onboarding from a home with no provider → custom endpoint → first chat (`chat…::Settings ›
  Providers re-runs onboarding …` against the real route; `surfaces…::first-run onboarding …` by key).
- [x] Packaged `--dir` build boots to an interactive window ≤ 60 s (`packaged…`).
- [x] Gap found: a reloaded conversation showed nothing against a real daemon (the WS does not
  serve `session.history`) — D-16.718, pinned by `src/chat/restHistory.test.ts` and the chat spec.
- [x] Gap found: the restart notice was never visible — D-16.720, `BootOverlay.test.tsx::a restart
  reported after recovery shows once as a toast …`.
- Deliberate differences: our own driver instead of Playwright; 1 retry on CI (the reference uses
  none; R10.20 asks for one, reported); Linux only for now.
- Not taken (follow-ups): clarify cards and the Deny path end to end; quit mid-turn with a running
  tool child under a process census; steer / queued follow-up / session switch / WebSocket drop
  mid-stream under the sampler; "one live socket per backend"; lineage (branch, rotation, compaction)
  specs; remote topology (remote chat is owed, D-16.691); two windows on one chat; the `known-bug`
  annotation mechanism; macOS and Windows lanes; signed installs (P23).

## Status

Done (2026-10-21) for the Linux lane: 6 spec files, all green locally under xvfb (see LEAD-NOTES
"P20 wave 35 landing notes (integrator)" for counts). Not runnable here: signed installs (P23), the
macOS and Windows lanes, a real global-shortcut keypress and the quick-entry window (no input
injection at the X server level; the registration state is asserted instead).
