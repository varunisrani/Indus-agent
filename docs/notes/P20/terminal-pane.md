# Task: Terminal pane (P20-31) Phase: 20 Owner: wave-35 builder D2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/terminal-ipc.ts`
(all: shell choice, env, session registry, start/write/resize/dispose, exit handling). Closed before
writing. For the env rules: our own `src/indus/safety/child_env.py` (the agent's env builder).
Evidence: none.

What it does (own words): the terminal pane runs real shells on this computer. Main owns the PTYs
(`node-pty`); the renderer draws them with xterm (WebGL renderer, falling back to the DOM renderer
when WebGL is missing or its context is lost). A rail lists the open terminals; a hidden terminal
keeps its shell and its screen (the pane body stays mounted, so its PTY and pid never change);
"Add selection to chat" hands the selected text to the composer. Closing a terminal whose shell is
still running asks first.

Main: `terminal.start {cwd?, cols, rows}` picks the shell (`INDUS_DESKTOP_SHELL`, else `$SHELL` on
POSIX, else zsh / bash / sh; on Windows pwsh, Windows PowerShell, then `COMSPEC`), starts it as an
interactive login shell in the requested folder when that folder is inside the window's allowed
roots (else the home folder), with an environment built by the same rules as the agent's env
builder: the provider, web-backend, messaging, GitHub and sandbox-vendor credential names of
`child_env.py`'s floor, every `*_API_KEY` name and every `INDUS_` name are removed (so
`OPENAI_API_KEY` set in the app's environment never reaches the shell); npm's injected settings and
colour-disabling variables are dropped; `TERM=xterm-256color`, `COLORTERM=truecolor`,
`TERM_PROGRAM=Indus`; on Linux `LC_CTYPE` falls back to `LANG` or `C.UTF-8` (a bare `UTF-8` makes
glibc warn), on macOS `UTF-8`. Output is batched (one IPC message per 8 ms burst) to the owning
window only; write/resize/dispose from another window are ignored; exit is reported and the PTY
released; a closed window disposes its terminals; quitting the app disposes all.

Inputs → outputs: start → `{terminalId, pid, shell, cwd}`; write / resize (send); dispose → kill;
events `terminal.onData {terminalId, data}`, `terminal.onExit {terminalId, exitCode, signal}`.

Files: `apps/desktop/electron/terminal/ptyHost.ts`, `apps/desktop/electron/terminal/terminalEnv.ts`,
`apps/desktop/electron/terminal/ptyHost.test.ts`, `apps/desktop/src/panes/terminal/TerminalPane.tsx`,
`apps/desktop/src/panes/terminal/xtermView.ts` (the lazy xterm wrapper),
`apps/desktop/src/panes/terminal/TerminalPane.test.tsx`. Shared: channel schemas +
`src/global.d.ts`, `electron/main.ts` (install line), `scripts/build-electron.mjs` (`node-pty` stays
external), `apps/desktop/package.json` (`node-pty` optional, `@xterm/*`).

Edge cases / failure modes:
- `node-pty` missing or not built → `terminal.start` answers `not-available` with a message; the pane
  shows it instead of a blank terminal;
- data arriving before the renderer learned the id → buffered per id in the pane until `start`
  answers;
- the shell exits → exit line shown, "Restart" offered; dispose of an exited terminal is a no-op;
- the owning window reloads or closes → its terminals are killed (no orphan shells);
- a cwd outside the roots, missing or a file → home folder (a file → its folder when allowed).

Our design differences / decisions:
- The env floor is mirrored from `child_env.py` (a test compares the two lists) plus every
  `*_API_KEY` name, because the desktop cannot read the profile's provider registry.
- No SSH terminals (remote connections get no local terminal pane: tools, terminals and files run on
  the remote host, R08.23).
- No web-links addon (a link in terminal output is not opened).

Tests we will write:
- env: `OPENAI_API_KEY` (and the floor, `*_API_KEY`, `INDUS_*`, npm settings) removed, locale fix
  per platform, parity of the floor with `child_env.py`;
- host with a fake PTY: shell choice, cwd confinement, owner-only write/resize/dispose, batching (fake
  clock), exit → event and release, window destroyed → killed, dispose kills; a real PTY run when
  `node-pty` is installed (skips otherwise);
- pane: start once, hide/show keeps the same terminal and pid, close guard, add selection to chat,
  `not-available` message.

## Decisions for the lead
- `terminal.start` answers `shell` and `cwd` beside `terminalId` and `pid`.
- `node-pty` is an optional dependency of the desktop (same pin as the TUI's, 1.1.0); its native
  build and `asarUnpack` are P20-38's.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Spawned shell env lacks `OPENAI_API_KEY` set in the parent (tests:
  `electron/terminal/ptyHost.test.ts::spawned shell env lacks OPENAI_API_KEY set in the parent`,
  `electron/terminal/terminalEnv.test.ts::spawned shell env lacks OPENAI_API_KEY set in the parent (and every other credential name)`,
  `::the floor equals the agent env builder's (child_env.py)`).
- [x] Hide/show keeps the same pty pid (tests: `ptyHost.test.ts::hide/show keeps the same pty pid (nothing in main restarts it)`,
  `src/panes/terminal/TerminalPane.test.tsx::hide/show keeps the same terminal and pid (no restart, no dispose)`).
- [x] Dispose kills the pty; exit releases it; a closed window and quit kill all (tests: `ptyHost.test.ts::dispose kills the pty; exit reports, flushes and releases`,
  `::a closed window kills its terminals; quitting kills all`; real PTY: `::runs a shell, lacks the parent's key, and dies on dispose` — skips without node-pty).
- [x] Shell choice: override, `$SHELL` (POSIX only), zsh/bash/sh with login flags; Windows pwsh →
  Windows PowerShell → COMSPEC with `-NoLogo` (tests: `ptyHost.test.ts::override, then $SHELL, …`, `::Windows: …`).
- [x] Linux locale fix-up; npm settings and colour-disabling variables dropped; `TERM`/`COLORTERM`/`TERM_PROGRAM` (test: `terminalEnv.test.ts::Linux locale fix-up: …`).
- [x] Output that arrives before the renderer knows the id is not lost (test: `TerminalPane.test.tsx::starts one shell in the session folder; early output is not lost; typing goes to it`).
- [x] Gap found: the reference lets any window write to or resize any terminal by id — ours only the owning window (`ptyHost.test.ts::write, resize and dispose only from the owning window`).
- [x] Close guarded; add selection to chat (tests: `TerminalPane.test.tsx::close is guarded while the shell runs; dispose kills it`, `::add selection to chat`).
- Deliberate differences: provider/credential names scrubbed (the reference keeps them); renderer-side
  buffering of early output instead of an attach handshake; output batched per 8 ms; at most 16
  terminals per window; no embedded-terminal marker variable (nothing reads one yet).
- Not taken: SSH terminals and remote PTYs (remote mode shows a notice; the serve's PTY bridge, P18-07,
  could back a remote terminal later), the MSYS bash defaults on Windows, restarting in the shell's
  last folder (`/proc/<pid>/cwd`), the dev-only `spawn-helper` +x repair (P20-38 packaging).

## Status

Done (2026-10-21). Shared: `apps/desktop/package.json` (`@xterm/xterm` 6.0.0, `@xterm/addon-fit`
0.11.0, `@xterm/addon-unicode11` 0.9.0, `@xterm/addon-webgl` 0.19.0, optional `node-pty` 1.1.0 — all
already in the lock for `apps/web` / `apps/tui`; lock +16 importer lines), `scripts/build-electron.mjs`
(`node-pty` external), channel schemas + `src/global.d.ts`, `electron/main.ts`, `electron/panesInstall.ts`.
No real PTY ran here (node-pty's native build needs node-gyp, absent in this container).
