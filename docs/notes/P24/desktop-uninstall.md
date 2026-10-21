# Task: Uninstall from Settings (P24-34) Phase: 24 Owner: wave-39 builder U3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/desktop-uninstall.ts`
(all 475 lines: modes → flags, install kinds, native-removal text, removable bundle path, the
POSIX and Windows cleanup scripts). Research 08 §3.29. Ours: `docs/internals/uninstall-levels.md`
and `src/indus/cli/uninstall.py` from `.agents/wip/w36/p23-integration-green.patch` (P23-20).
Closed before writing.
Evidence: none.

What it does (own words): Settings → About gets an "Uninstall" section with three levels that are
exactly the CLI's (`docs/internals/uninstall-levels.md`):

| Level (UI) | Command line the helper runs |
|---|---|
| App only | `indus uninstall --gui --yes` |
| App and runtime, keep my data | `indus uninstall --yes` |
| Everything | `indus uninstall --full --yes` |

The app never deletes anything itself. When the person confirms, main writes a small helper script
to a temp folder, starts it detached, and quits; the helper waits (bounded, 30 s) for the app's
pid to exit, then runs the command line above with the runtime's Python (`<python> -m indus
uninstall …`, `INDUS_HOME` set to the app's home), then deletes itself.

**Ownership.** The options are shown only when ownership is confirmed from the install receipt:
- the CLI's summary (`indus uninstall --gui-summary`, JSON) answers, its `install_kind` is not a
  managed kind (`nix`, `docker`, `desktop` — the bundled runtime) and `agent_installed` is true;
- an installer receipt (`<home>/logs/install_receipts/*.json`) with `outcome: "success"` exists;
- the app itself is not under a Nix store path (`/nix/store/…`) and its build stamp does not say an
  external manager updates it.
Anything missing, unreadable or contradictory hides every option and shows why (the managed
kind's instruction, or "ownership could not be confirmed"). Main answers the summary; the renderer
never builds a command line.

**Confirmation.** Every level asks once ("Indus quits and …"); "Everything" additionally needs the
person to type `uninstall everything` exactly; without it the run request is refused in main as
well as in the UI (main checks the typed phrase it receives).

Inputs → outputs: `indus:uninstall:summary` → `{allowed, levels[], reason?, home, installKind}`;
`indus:uninstall:run {level, typed?}` → `{ok:true}` (then the app quits) or `{ok:false, code}`.

Files: `apps/desktop/electron/uninstall.ts`, `apps/desktop/electron/uninstall.test.ts`,
`apps/desktop/src/settings/UninstallSection.tsx`, `apps/desktop/src/settings/UninstallSection.test.tsx`,
`apps/desktop/src/settings/uninstallStrings.en.ts`. Shared: `electron/ipc/channels.ts` +
`schemas.ts` (2 invoke channels), `src/global.d.ts`, `electron/main.ts` (one install step: the
resolver's `indus`, `runCommand`, receipts reader, helper writer in the temp dir (0700, `wx`),
detached spawn behind `machinery.guard`, `app.quit`), `src/settings/customPages.tsx` (About page
renders the section), `src/settings/pages.ts` (search entry), `src/settings/context.tsx`
(`uninstall` in `SettingsBridge`), `src/i18n/en.ts` (strings spread).

Edge cases / failure modes:
- summary command missing / exits non-zero / prints non-JSON / times out (10 s) → hidden;
- install kind `nix` → hidden with the Nix instruction;
- no success receipt → hidden ("ownership could not be confirmed");
- level not in the list, or "everything" without the exact phrase → refused, nothing spawned;
- paths with spaces or quotes in the helper → single-quoted (POSIX) / quotes stripped + refused when
  they hold `"`, `%`, `^`, `&` (Windows);
- a second run while one is pending → refused (`busy`).

Our design differences / decisions:
- Levels are the CLI's names (`gui`, `keep-data`, `full`); the `--data` level is not offered in the
  app (it is for externally managed installs, where the app hides everything).
- The helper runs only the CLI; it does not delete the app bundle on its own — the CLI's `gui` level
  removes the desktop build in the checkout and the app's user data.
- Typed phrase for "Everything" checked in main, not only in the UI.

Tests we will write:
- argv per level matches the flags table (and nothing else is accepted);
- Nix-owned receipt hides all options; docker/desktop kinds; missing receipt; bad JSON → hidden;
- "everything" without / with a wrong typed confirmation does nothing (no spawn); with it spawns;
- POSIX and Windows helper text: waits for the pid, sets INDUS_HOME, runs the argv, self-deletes;
- UI: options hidden with the reason; typed-confirm gate on the button.

## Decisions for the lead
- Python for the helper: the runtime Python the backend resolver found (`descriptor.python` when
  known, else `python3`/`py -3` on PATH). On Windows a venv python deleting its own venv can
  half-fail; the reference uses a system Python — follow-up if Windows keep-data shows it.
- Confirmation phrase `uninstall everything`.

Compare checklist (filled after the rewrite; reopened the file in `Read:`):
- [x] three levels ↔ flags; unknown level refused (test: `electron/uninstall.test.ts::the helper command line matches…`)
- [x] managed kinds (nix, docker, bundled desktop runtime), Nix store path, no receipt, bad summary hide every level (tests: `::a Nix-owned receipt hides all options`, `::%s hides every level`)
- [x] "everything" needs the typed phrase, checked in main (tests: `::"everything" without the typed confirmation does nothing`, `UninstallSection.test.tsx`)
- [x] helper waits ≤ 30 s for the pid; Windows exact PID match; quoting; self-delete (test: `::helper scripts`)
- [x] ownership re-checked at run time (test: `::ownership is checked again in main…`)
- Deliberate differences: the helper runs only the CLI; levels named as the CLI's; no `data` level.
- Not taken: Windows "Apps & Features" deep link; removing a packaged bundle outside the checkout;
  a system Python for Windows keep-data/full (follow-up in Decisions).

## Status

Done (2026-10-21). Shared files listed under Files.
