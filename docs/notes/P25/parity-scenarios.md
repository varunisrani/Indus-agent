# Task: Product scenarios (P25-07) Phase: 25 Owner: wave-41 builder Q1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — skill reference 03 §2 (the
scenario rules) and REA's own `capture_browser_scenario` contract as REA's MCP server published it
in this session (a third-party tool contract; recorded in `rea-version.md`). No reference product
file was read: the step lists come from our own product (its prompts, hooks and e2e specs).
Evidence: `ev_417537dd…aa9`, `ev_a7e0821d…273`, `ev_04e715c1…044` (REA runs of our dashboard).

What it does (own words): one file per flow, `parity/<flow>.indus.json`, with stable step ids that
the reference-side files (kept in `~/rea-lab/parity/`, never committed) reuse, so REA can align
the two captures step by step. Twelve flows: `first-run-setup`, `cli-chat-approve`,
`cli-resume-search`, `tui-stream-interrupt`, `tui-model-picker`, `dashboard-sessions`,
`dashboard-chat-tab`, `desktop-onboarding`, `desktop-chat-panes`, `gateway-telegram-approve`,
`cron-schedule-run`, `acp-session`.

- Browser files are REA input as they are: `browser` (a placeholder the runner fills), `start_url`
  on the canonical origin, the shared `environment` (1280×720, `en-US`, UTC, light, reduced motion,
  service workers blocked), the shared `capture` (url, accessibility and screenshot after each
  step; url and DOM at the end; console, page errors and network), `actions`. Every locator is a
  CSS selector on one of our hooks (`[data-testid="…"]`, a `^=` prefix for rows that carry an id,
  or `[data-ui="…"]`), waits are for states (`wait_for … visible`), never for time.
- Terminal, stdio and platform files use our step list (`format: indus-terminal-scenario/1`):
  `launch` (argv starting with `indus`), `wait_for` (text, card, prompt, file / message, response /
  platform), `type`, `press`, `send`, `close_input`, `inbound`, `press_button`, `stop`, `wait_exit`.
- Every step has `timeout_ms`; step ids match REA's pattern and are unique; secrets are referenced
  by `secret_id` and declared in `secrets` (none of the twelve needs one yet).

Inputs → outputs: the files → `runner.scenario_problems` / `scenarios.rea_browser_problems` /
`scenarios.terminal_problems` / `scenarios.hook_problems` / `scenarios.secret_problems` (empty when
sound); the runner refuses a flow whose file fails.

Files: `parity/*.indus.json` (12), `tests/unit/parity/test_scenarios.py`.

Edge cases / failure modes:
- a locator that matches a container instead of the control (first REA run: the session row `li`
  took the click, the detail never opened) — pinned to the row's toggle button;
- the start URL redirects (`/` → `/sessions`): steps wait for hooks, not URLs;
- hooks the product lacks: the desktop onboarding overlay, the desktop transcript's tool row and
  the pane toggles have no hook yet. They are listed in `scenarios.OWED_HOOKS` with a reason; the
  self-test fails for an unknown hook and for an owed hook that has appeared (drop it then).

Our design differences / decisions:
- Hooks: the product carries `data-testid` (P18/P20), not `data-ui`; both attributes count as our
  hooks, and new ones should use `data-ui` (the owed list does).
- `wait_for_timeout` is refused in our files (REA allows it without a timeout).

Tests we will write: one file per flow; each file valid (contract, ids, timeouts); browser hooks
resolve in the app source or are owed; owed hooks are used and still missing; shared environment
and capture; canonical origin; seeded violations each refused (unknown key, action, state,
locator kind, step id, URL, event, missing browser; duplicate id; missing timeout; time waits;
non-hook locator; unknown hook; undeclared secret; terminal argv, locator kind, key, action).

## Decisions for the lead
- `OWED_HOOKS` (9, desktop): `onboarding-dialog`, `onboarding-custom-option`,
  `onboarding-endpoint-url`, `onboarding-test-continue`, `onboarding-model-select`,
  `onboarding-start-chatting`, `transcript-tool-row`, `pane-toggle-files`, `pane-toggle-review`.
  P24 changes the desktop files, so they are owed, not added here; add them as `data-ui` after P24
  lands (the scenario self-test then asks to drop them from the list).
- Desktop scenarios use REA's connect mode (`cdp_endpoint` placeholder `http://127.0.0.1:9222`,
  target `indus-renderer`) and the renderer served on the canonical origin; the owner lane fills the
  real endpoint.

Compare checklist (filled after the rewrite; reopened skill reference 03 §2 and REA's contract):
- [x] fixed action vocabulary, no hover/force (`tests/unit/parity/test_scenarios.py::test_rea_contract_violations_are_refused`)
- [x] every step has `timeout_ms` (`::test_step_ids_are_unique_and_every_step_has_a_timeout`)
- [x] locators match one element, on our hooks (`::test_browser_locators_use_our_hooks_and_resolve`; strict mode proven by REA's runs)
- [x] identical environment and capture blocks (`::test_browser_files_share_one_environment_and_capture_block`)
- [x] secrets declared by id (`::test_secrets_are_declared_by_id`)
- [x] files validate against REA's contract and ran through REA (`::test_scenario_is_valid`; evidence above)
- Deliberate differences: `data-testid` accepted as our hook (see decisions).
- Not taken: reference-only steps (their file, in `~/rea-lab`, adds them with its own prefix).

## Status

Done (2026-10-21). 12 files; 10 run green here (2 desktop files validated, not run).
