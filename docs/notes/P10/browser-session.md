# Task: browser session manager (P10-12) Phase: 10 Owner: wave-1 builder I

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/browser_tool_session.py`
(session records per task key, creation precedence CDP > local, suspect/recycle after a command
timeout, sandbox-bypass flags, missing-Chromium hint, preflight), `tools/browser_tool_lifecycle.py`
(janitor loop every 30 s, idle reaping under the owner's scope, failure counter and force reap,
orphan reaper on start and every 300 s with a grace period, emergency cleanup), `tools/browser_tool.py`
lines 47–260 and 389–424 (scrubbed env, timeouts and their floors, inactivity constants),
`tools/browser_tool_install.py` (skimmed: Chromium detection, docker detection). Closed before
writing. Evidence: none.

What it does (own words): every task id gets its own Chromium (a persistent Playwright context in
its own profile folder), started headless on first use with an environment built from the
owning `Scope` (the `code` variant of `child_env`: no credential-named variables, no provider
keys), plus the names listed in `browser.env_passthrough` read from the scope's secrets/env. The
Chromium sandbox stays on unless it cannot work (running as root, inside a container, or the
kernel forbids unprivileged user namespaces); then the sandbox is off and
`--disable-dev-shm-usage` is added. When a CDP endpoint is chosen (`/browser connect`, else
`INDUS_BROWSER_CDP_URL`, else `browser.cdp_url`) the session attaches to that browser instead of
launching one. Every command runs under a deadline (`browser.command_timeout`, 30 s, floor 5 s;
navigation at least 60 s, the first navigation of a session at least 120 s). A command that times
out marks the session suspect; the next use closes it and starts a fresh one. A janitor (every
30 s on the event loop that owns the sessions) closes sessions idle longer than
`browser.inactivity_timeout` (120 s, floor 30 s), re-entering each session's own scope; a close
that fails three times in a row is force-reaped (the handle is dropped and the profile's
processes killed). The orphan reaper (at the first janitor pass and every 300 s) removes profile
folders left by a dead Indus process, and folders of a live process that this manager does not
track once they have been idle for max(3,600 s, 20 × inactivity), killing any Chromium still using
them. A missing Chromium is an error naming `indus tools install browser`.

Inputs → outputs: `SessionManager.session(task_id, scope)` → `BrowserSession` (page driver,
supervisor, mode, ref attribute); `run(session, op, timeout_s=…)` → op result or `CommandTimeout`
/ `DialogBlocking`; `close(task_id)`, `janitor_tick()`, `reap_orphans()`, `shutdown()`,
`release(task_id)`. Settings: `browser.inactivity_timeout`, `browser.command_timeout`,
`browser.cdp_url`, `browser.executable_path`, `browser.env_passthrough`. Files: profile folders
`<home>/cache/browser/profiles/<name>/` with `owner.json` (`pid`, `task`, `created`).

Files: `src/indus/tools/builtin/browser/{__init__,session,driver}.py`, `tests/fakes/browser.py`,
`tests/unit/tools/builtin/browser/test_session.py`; shared: `tools/toolsets.py` (toolset
`browser`, core membership), `tools/builtin/__init__.py` (module list),
`foundation/config/schema.py` (`browser` section) and its test, `pyproject.toml` (extra
`browser`, marker `browser`).

Edge cases / failure modes:
- Playwright not installed → toolset probe says no (unless a CDP endpoint is configured, which
  still needs Playwright: the call then names the extra).
- Executable missing → Playwright's "Executable doesn't exist" becomes our install hint.
- Two calls for the same new task race → one creation lock per task id; the loser reuses.
- Close raises (dead driver) → counted; third failure force-reaps; a success resets the count.
- Command times out while a JavaScript dialog is open → not a dead session: `DialogBlocking`,
  the caller shows the pending dialog instead of recycling.
- The CDP choice changes (`/browser connect`) → the next use of each session sees a different
  mode key and replaces it.
- Orphan scan on a host without `/proc` → folders are still removed after the grace period; no
  process kill is attempted.

Our design differences / decisions:
- Playwright (Python) drives Chromium directly; no separate browser CLI or daemon, no socket dirs.
  The orphan record is the profile folder and its `owner.json` instead of a daemon pid file.
- One persistent context (its own Chromium) per task id rather than contexts inside a shared
  browser: the env and profile belong to the task's scope, and a stuck page cannot block others.
- Chromium's own sandbox stays on when it can work (Playwright turns it off by default).
- No cloud providers, Camofox, Lightpanda, real-profile or hybrid sidecar modes (P3, see phase).
- `/browser` state is per profile home (one profile's connect never moves another's browser).

Tests we will write:
- fake clock: idle 121 s reaped, 119 s kept; three failed closes then force reap; success resets.
- timed-out command marks suspect → next `session()` builds a new one, old one closed.
- launch env lacks provider keys and credential names; passthrough names come from scope secrets.
- missing executable → hint; Playwright absent → hint naming the extra; probe false.
- two task ids → two launches, two profile folders; same id → one launch.
- sandbox decision table; timeouts and floors; orphan reaper with a fake process table.

## Decisions for the lead
- Pin `playwright==1.63.0` (uploaded 2026-09-15, Apache-2.0; deps `greenlet` MIT/PSF, `pyee`
  MIT). It expects Chromium build 1243; machines with another build set `browser.executable_path`
  (the integration test finds `$PLAYWRIGHT_BROWSERS_PATH/chromium` itself).
- `browser.env_passthrough` (default empty) is the "browser keys" list; values come from the
  scope's secrets, then its env, and blocked names are refused like `terminal.env_passthrough`.
- Browser tools are in `CORE_TOOLS` (probe-gated), as in the reference.
- Host wiring: call `await indus.tools.builtin.browser.DEFAULT_TOOLS.manager.shutdown()` at exit
  (next to the terminal runtime's shutdown) and `DEFAULT_TOOLS.manager.release(task_id)` from the
  P10-06 child teardown; both are safe when no browser was used.
- Intended differences for D-ids: Playwright in-process instead of a browser CLI daemon; one
  Chromium per task id; sandbox kept on where it works; no cloud/Camofox/Lightpanda/real-profile
  backends; `/browser` state per profile home.

Compare checklist (filled after the rewrite; reopened `tools/browser_tool_session.py`,
`tools/browser_tool_lifecycle.py`, `tools/browser_tool.py` 136–425):
- [x] janitor every 30 s, idle past the limit closed under the owner's scope (tests:
  `test_session.py::test_idle_session_is_reaped_after_the_inactivity_limit`,
  `::test_teardown_re_enters_the_owning_scope`, `::test_janitor_loop_runs_on_the_fake_clock`)
- [x] failure counter, force reap at 3, only a success resets it (`::test_three_failed_closes_force_reap`,
  `::test_a_successful_close_resets_the_failure_count`)
- [x] orphan reap at the first pass and every 300 s (10 janitor passes), grace
  max(3,600, 20 × inactivity), dead owner reaped at once, folder without owner only after grace
  (`::test_orphan_reaper`, `::test_timeouts_and_floors`)
- [x] command timeout marks suspect, recycled on next use (`::test_timed_out_session_is_recycled`)
- [x] timeouts 30 (floor 5) / open 60 / first open 120 (`::test_timeouts_and_floors`)
- [x] credential-scrubbed env with only the listed browser names from the scope
  (`::test_launch_env_lacks_provider_keys_and_credentials`, `::test_browser_env_is_built_from_the_scope_only`)
- [x] sandbox bypass for root, containers and restricted user namespaces (`::test_sandbox_decision_table`)
- [x] missing Chromium → install hint (`::test_missing_browser_names_the_install_command`; integration
  `test_browser_local.py::test_missing_executable_gives_the_install_hint`)
- Deliberate differences: the reference runs a browser CLI daemon per session and recognises
  orphans by socket folders and daemon pid files; we drive Playwright in-process and recognise
  orphans by profile folders (`owner.json`) and the `--user-data-dir` argument of the processes.
  A timed-out CDP session is replaced on next use like a local one (the reference swaps the CDP
  client at once). Chromium's sandbox stays on when it can work.
- Not taken: cloud providers and their fallback to local, Camofox, Lightpanda, real-profile mode,
  the hybrid private-URL sidecar, a shared headed browser leased to a person, session recordings,
  sandboxed browsers inside the terminal backend (P3 / P24, see the phase file).

## Status

Done (2026-10-19). Shared files: `pyproject.toml` (extra `browser`, marker `browser`), `uv.lock`
(regenerated here; the lead re-locks), `THIRD_PARTY_NOTICES.md` (playwright, greenlet, pyee),
`tools/toolsets.py` (`BROWSER_TOOLS`, toolset `browser`, core list), `tools/builtin/__init__.py`
(module list), `foundation/config/schema.py` (`browser` section) and its test,
`tests/unit/tools/test_toolsets.py`, `tests/unit/cli/setup/test_wizard.py` (new core row),
the prompt-surface goldens (`tools.json`, note in `docs/notes/P04/golden-prompts.md`),
`docs/internals/traps-ledger.md` (R02.22 row).
