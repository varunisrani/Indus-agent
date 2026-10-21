# Interop record: REA for the parity pass (P25)

REA (rea-agents) is a third-party tool. Registration is settled (D-16.61: owner approved
2026-10-18; rea-agents 6.7.0 registered). This record says which contract our scenario files are
checked against and what ran through REA.

## Version and contract

- Package: rea-agents 6.7.0 (HANDOFF, D-16.61). The MCP server used on 2026-10-21 reports its
  browser provider as `rea-playwright-browser-scenario` version `1`, driving Chromium
  141.0.7390.37 (`/opt/pw-browsers/chromium-1194`).
- `capture_browser_scenario` contract as that server published it on 2026-10-21 (tool input
  schema): top-level keys `browser` (required; `launch` with an absolute `executable_path` and
  optional `headless`, or `connect` with `cdp_endpoint` and `target_id`), `start_url` (required;
  `url`, `query`), `actions` (required, non-empty), `capture`, `environment`, `secrets`,
  `storage`; unknown keys refused. Actions: `goto` (`destination`, `wait_until`), `click`
  (`button`, `click_count`), `fill` (`value`), `press` (`key`), `select_option` (`value`),
  `check`/`uncheck`, `wait_for` (`state`: attached, detached, visible, hidden), each with an
  optional `timeout_ms`; `wait_for_timeout` (`duration_ms`, no `timeout_ms`). Step ids match
  `^[A-Za-z][A-Za-z0-9._-]*$`. Locators: `test_id` (`value`), `role` (`role` from a fixed list,
  exact `name`), `css` (`selector`). Values: `{source: literal, value}` or `{source: secret,
  secret_id}`; secrets declared as `{secret_id, environment_variable}`. Capture: `after_each_step`
  / `at_end` from screenshot, dom, accessibility, url, history, storage; `events` from console,
  page-errors, network, websockets, frames, workers, popups, downloads. Environment: viewport,
  locale, timezone, color scheme, reduced motion, service workers. No `allowed_origins` and no
  launch permission ceiling in this version.
- `scripts/parity/scenarios.py` (`rea_browser_problems`) encodes these rules in our own code; when
  REA is upgraded, compare its published schema with this list and update both.

## Runs through REA (our product only)

| Date | Flow | Evidence | Result |
|---|---|---|---|
| 2026-10-21 | dashboard-sessions | `ev_417537dd7a62e2fd976a5df4bb4900d30643a3529b76abf46f3e85fe5ee9aaa9` | contract accepted; 5 of 6 steps; `open-session` clicked the row container, the detail never opened (locator fixed) |
| 2026-10-21 | dashboard-sessions | `ev_a7e0821db26f578e03207844a667bc913b0bd6e78548920c1924d0def010b273` | 6 of 6 completed, no page error |
| 2026-10-21 | dashboard-chat-tab | `ev_04e715c19c978f3b29137432387fae32bf696623efc895787df94acbaaaf2044` | 10 of 10 completed, no page error |

Those runs requested lighter capture blocks than the files (URL and accessibility, or URL only)
to keep the MCP answers small; the files' own capture blocks are what the owner's runs use. The
Evidence stays with REA / `~/rea-lab`; only the ids are cited here.

## Not run through REA

- Terminal, stdio and platform flows: our transcript driver (P25-05 row).
- Desktop flows: need the packaged app and REA's connect mode (owner lane).
- Every reference-side capture: the owner's REA sandbox (`~/rea-lab`); the reference app is never
  run or installed in builder sandboxes.
