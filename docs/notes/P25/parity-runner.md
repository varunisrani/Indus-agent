# Task: Parity runner, product side (P25-05) Phase: 25 Owner: wave-41 builder Q1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — the row reads only the
clean-room skill's reference 03 §1–§3 (process: what lives where, scenario files, one server per
scenario). No reference product file was opened; the reference app was never run or installed.
Evidence: REA captures of **our** dashboard made through REA's MCP tools in this session (see
`rea-version.md`): `ev_417537dd…aa9`, `ev_a7e0821d…273`, `ev_04e715c1…044`.

What it does (own words): runs every parity flow on Indus Agent, strictly one at a time. Each flow
gets a fresh temp folder with its own Indus home, a fresh user home, a demo git project (with a
bare `origin`) or an empty folder, and its own scripted model server (P25-06) that the home's
config points at (`provider: custom`, `base_url`, `default: <script>`). Everything the child
processes see comes from a clean environment (PATH, the two homes, `LANG=C.UTF-8`, `TZ=UTC`,
`TERM`, a null keychain backend, a private git config, a per-flow terminal id) — nothing of the
caller's shell leaks in. The setup lives in the runner's `FLOWS` table (`--list` prints it),
because REA's scenario schema has no room for it.

- **Terminal flows** (classic CLI, setup wizard, TUI, cron) run in a pseudo-terminal through our
  transcript driver (`terminal_driver.py`): a small screen model (cursor moves, erase, scroll
  regions, alternate screen, pending wrap, scrollback, cursor-position replies) turned into
  structured events per step: regions, cards (surface-specific markers), prompts, exit codes.
- **Stdio flows** (`indus acp --local`): newline-delimited JSON-RPC; waits match responses (by the
  sending step) and notifications (method + text); results feed later steps (`$result`).
- **Platform flow** (gateway): the real `indus gateway run` against a loopback stand-in for the
  Telegram Bot API (`telegram_fake.py`, reached through the adapter's `base_url`); steps inject a
  direct message, wait for the bot's messages, press inline buttons, stop with SIGTERM.
- **Browser flows** (dashboard, desktop renderer): the scenario file is REA's
  `capture_browser_scenario` input; the runner rewrites the canonical origin
  `http://127.0.0.1:24000` to `--port`, fills the local browser, writes it to the cache and hands
  it to `--rea-cli` (a command taking the input path and printing REA's result). REA here is
  reachable only through its MCP tools, so `--serve FLOW` sets a dashboard flow up and keeps the
  server running until stdin closes, and `--import FLOW RESULT` stores REA's result as the capture.

Captures go to `.cache/parity/indus/<flow>.capture.json` (git-ignored; format
`indus-parity-capture/1`: flow, side, driver, versions, status, reason, per-step status, error,
elapsed time and what was observed). A failed step marks the rest `not_run`; a broken scenario
fails that flow and the run moves on; exit 1 when a flow failed or a divergence is unclassified.

Inputs → outputs: `python scripts/parity/runner.py --list | FLOW… | --all [--dry-run] [--port N]
[--browser PATH] [--rea-cli CMD] [--ref DIR] [--ledger] [--serve FLOW] [--import FLOW FILE]` →
per-flow line `passed|failed|blocked|not_run flow (reason)`, captures and prepared REA input in
the cache, comparisons when `--ref` names reference captures, the ledger report with `--ledger`.

Files: `scripts/parity/runner.py`, `scripts/parity/terminal_driver.py`,
`scripts/parity/scenarios.py` (scenario loading and validation, split out of the runner),
`scripts/parity/telegram_fake.py`, `scripts/parity/__init__.py`, `tests/unit/parity/test_runner.py`;
shared: `.gitignore` (`.cache/parity/`).

Edge cases / failure modes:
- line editors batch fast input (prompt_toolkit read "text\r" sent in one write as a paste, so the
  turn never started): the driver leaves 200 ms between typed text and a key;
- prompt_toolkit asks for the cursor position; without an answer it warns and redraws, so the
  screen model answers CSI 6n (and primary device attributes) like a terminal;
- `indus` attaches to the profile daemon by default (D-16.487) and starts one: terminal flows use
  `--local`; `cleanup` still runs `indus gateway stop` on the flow's home when a daemon left files;
- `indus -c` follows a per-terminal breadcrumb (TTY name): each flow pins `INDUS_TERMINAL_ID`;
- processes are stopped by their own process group only (never by pattern); temp folders removed
  unless `--keep`.

Our design differences / decisions:
- Terminal flows are not run through REA: its process capture takes input at fixed times, the
  row asks for our transcript driver. The reference's terminal captures can be made with the same
  driver (it only needs a command line), or with REA's process capture by the owner.
- The gateway flow is driven through the real gateway process and a Bot API stand-in instead of
  the in-process fake platform of the e2e suite, so the product side is the shipped command.

Tests we will write: `--list` prints every flow; every flow names a known script and has a
scenario; a dry run completes the CLI flow and writes a capture; a missing step id fails that flow,
not the run; a failing step marks the rest not run; browser flows prepare REA input with the origin
rewritten; REA result import; clean environment; screen model (moves, erase, wrap, scrollback,
alternate screen, split escapes, wide characters, cursor report); structure (regions, cards,
prompts); key names; the Bot API stand-in (updates, buttons, presses, long poll).

## Decisions for the lead
- Flows that cannot run here are `not_run` with the reason (desktop: packaged app + REA's CDP
  connect mode, owner lane); browser flows without `--rea-cli` are `blocked` and leave their
  prepared input in the cache.
- Captures of browser flows made through REA's MCP tools are imported with `--import`; the runner
  does not talk MCP itself (no client library, and the brief allows no new dependency).
- The temp folders live under the system temp directory (TMPDIR as the caller sets it).

Compare checklist (filled after the rewrite; reopened skill reference 03 §1–§3):
- [x] one server per flow, set up like its e2e spec, table printed by `--list` (`tests/unit/parity/test_runner.py::test_list_prints_every_flow`)
- [x] canonical origin 24000, rewritten for `--port` (`::test_browser_flows_prepare_reas_input_in_a_dry_run`)
- [x] strictly one flow at a time (the run loop; each flow's server stops before the next starts)
- [x] verdict: a flow passes when every step completed and the page threw no uncaught error
  (`::test_import_turns_a_rea_result_into_a_capture`)
- [x] captures in a git-ignored cache (`.gitignore`)
- [x] run through REA itself on day one: both dashboard flows ran through REA's
  `capture_browser_scenario` (all steps completed; the first run caught a locator that clicked the
  row instead of its toggle, fixed in the scenario)
- Deliberate differences: terminal and stdio flows use our transcript driver (the row asks for it).
- Not taken: launching the desktop renderer (owner lane, packaged build).

## Status

Done (2026-10-21). Runs green here: first-run-setup, cli-chat-approve, cli-resume-search,
tui-stream-interrupt, tui-model-picker, gateway-telegram-approve, cron-schedule-run, acp-session
(runner) and dashboard-sessions, dashboard-chat-tab (through REA's MCP tools). Desktop flows not run.
