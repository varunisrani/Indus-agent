# Task: alternative engines and `browser_exec` (P24-39) Phase: 24 Owner: wave-38 builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/browser_lightpanda.py`
(binary lookup, `serve` on a free loopback port, readiness wait, stderr tail, stop and orphan
records), `tools/browser_tool_lightpanda_fallback.py` (the fallback-eligible command set, the
reasons a light result needs Chrome, the temporary Chrome session), `tools/browser_camofox.py`
(lines 1–500: REST paths, identity, tab lifecycle, snapshot parsing, auth header),
`tools/browser_use_cli.py` (command shape only: argv, session names, timeout clamp, process-group
kill, stderr cap, workspace). Closed before writing. Evidence: none.

Vendor interfaces pinned: Lightpanda `lightpanda serve --host 127.0.0.1 --port <n>` (nightly
builds of 2026; CDP discovery at `http://127.0.0.1:<n>/json/version`). Camofox server REST API (the
camofox-browser project's documented routes): `GET /health`, `POST /tabs {userId, listItemId,
url}` → `{tabId}`, `POST /tabs/{id}/navigate {userId, url}`, `GET /tabs/{id}/snapshot?userId=` →
`{snapshot, refsCount}`, `POST /tabs/{id}/{click|type|press|scroll|back}`, `GET
/tabs/{id}/screenshot?userId=` (PNG bytes), `POST /tabs/{id}/evaluate {userId, expression}`,
`DELETE /sessions/{userId}`; optional `Authorization: Bearer` from `CAMOFOX_API_KEY`. The
external automation CLI for `browser_exec` is `agent-browser` (Vercel Labs, Apache-2.0, a separate
program never imported): `agent-browser --session <name> --json <command> [args]` with commands
`open <url>`, `snapshot`, `screenshot [path]`, `eval <js>`, `click <ref>`, `fill <ref> <text>`,
`press <key>`, `scroll <dir>`, `back`.

What it does (own words): `browser.engine` picks the local engine: `chromium` (P10 default),
`lightpanda` or `camofox`. Lightpanda is a fast, display-less engine: we start one `lightpanda
serve` per task on a free loopback port with the scope's credential-free browser environment
(R02.22), wait up to 10 s for its CDP answer, and attach to it like a CDP endpoint. Lightpanda has
no renderer, so a screenshot always goes to a Chrome fallback (a temporary local Chromium opened on
the same address, guarded like any session, closed right after); one of the fixed fallback
commands (open, snapshot, screenshot, eval, click) that fails on Lightpanda, or a snapshot that
comes back nearly empty, is retried the same way, and the result says Chrome served it. Camofox is a
stealth engine run as a separate local server; we drive it over its REST API with a stable per-
profile identity kept in its own state folder (`<home>/cache/browser/camofox/`). Each engine is
optional: a missing binary or a server that does not answer gives an install hint, never a crash.

`browser_exec` runs a short scripted step list (`open`, `snapshot`, `screenshot`, `eval`,
`click`, `type`, `press`, `scroll`, `back`) through the external `agent-browser` CLI when it is
installed (found through `indus pm`'s `tool_path`, then the scope's PATH), one CLI call per step
under one named session, with the scope's credential-free environment. URLs in `open` steps and
URL literals in `eval` code pass the P05 URL policy first. Each step's output is cut to 4,000
characters, the whole result is redacted and registered with a result cap so the tool round spills
it to a file past the budget like any other tool's output.

Inputs → outputs: `browser.engine`, `browser.camofox_url`, `CAMOFOX_API_KEY`; `browser_exec {steps,
session?, timeout_s?}` → JSON `{success, steps:[{action, ok, output|error}], session, engine}`.

Files: `src/indus/tools/builtin/browser/engines.py`, `src/indus/tools/builtin/browser/exec_cli.py`,
tests `test_engines.py`, `test_exec_cli.py`; shared: `browser/session.py` (engine dispatch),
schema rows `browser.engine`, `browser.camofox_url`.

Edge cases / failure modes:
- no `lightpanda` binary → "install Lightpanda (`indus pm install lightpanda` once packaged, or put
  `lightpanda` on PATH), or set browser.engine to chromium"; Windows has no build;
- `serve` exits before it answers → the stderr tail (redacted) in the error; never answers in 10 s
  → the process is stopped and the error says so; the server is stopped on every session exit;
- Chrome fallback missing Chromium → the P10 install hint; the temporary profile folder is removed;
- Camofox server down → "start the Camofox server" hint; a 404 on a tab means it was collected:
  a new tab is opened;
- `browser_exec` with no CLI → install hint; a step that times out kills the CLI's process group and
  stops the remaining steps; an invalid session name, empty step list, unknown action or more than
  25 steps → refused before anything runs; a refused URL stops the call before the CLI runs.

Our design differences / decisions:
- `browser_exec` takes a fixed step list, not free-form Python (the reference runs model-written
  Python through its harness): a fixed command set keeps the URL policy enforceable.
- The external CLI comes from `indus pm` / PATH; we never bundle it.
- Camofox's sub-resource requests cannot be guarded (it is a separate server); navigations are
  checked before and after like an attached browser.

Tests we will write:
- screenshot on the light engine routes to Chrome; a failing eligible command falls back; an
  ineligible one does not; nearly empty snapshot falls back; Lightpanda env has no provider keys;
  missing binary hint; readiness timeout stops the process; server stopped at close;
- Camofox: navigate/snapshot/click over `respx`, identity file in its state folder, 404 tab recreated;
- `browser_exec`: missing CLI hint; argv shape and env; refused URL; per-step cap; timeout kill;
  output spilled past the budget.

## Decisions for the lead
- `pm` has no `lightpanda` / `agent-browser` packages yet: add them to `pm/packages.toml` at
  integration (the code asks `indus.pm.tool_path` first, then PATH).
- `browser_exec` registers in toolset `browser` only when the CLI is found (probe); the lead decides
  whether it joins `BROWSER_TOOLS` (golden tool arrays change).

Compare checklist (filled after the rewrite; reopened the files above):
- [x] fallback-eligible set and the reasons (failure, near-empty snapshot, screenshot) (tests: `test_engines.py::test_screenshot_on_lightpanda_goes_to_chrome`, `::test_failed_eligible_command_falls_back`, `::test_nearly_empty_snapshot_falls_back`)
- [x] readiness wait 10 s and stop on timeout (test: `test_engines.py::test_server_that_never_answers_is_stopped`)
- [x] stderr capped and redacted; timeout clamp 5–1800 s (tests: `test_exec_cli.py::test_step_output_is_capped`, `::test_timeout_is_clamped`)
- [x] Camofox tab recreated after a 404 (test: `test_engines.py::test_camofox_recreates_a_collected_tab`)
- Deliberate differences: step list instead of Python; no shared-tab preamble; no BU cloud autospawn.
- Not taken: orphan records for Lightpanda servers across processes (each server dies with its
  session and its process group), the HTTP cache flag probe, Camofox VNC links and managed identity
  overrides, Docker loopback rewriting.

## Status

Done (2026-10-21).

## Integration (P24 integrator, 2026-10-21)

Wired: `browser_exec` registers with the browser tools and is in `BROWSER_TOOLS` (golden arrays
regenerated). Lightpanda and agent-browser are not in `indus pm`'s catalogue (Lightpanda is
AGPL-3.0; agent-browser is npm-only): found on PATH only (owner, D-16.802).
