# Task: First-run guard + expensive-model guard (P07-03) Phase: 07 Owner: P07 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/main.py`
(`_has_any_provider_configured`, `_first_run_setup_guard`,
`_confirm_startup_expensive_model_override`, their call order in `cmd_chat`),
`hermes_cli/model_selection_guards.py` (guard registry shape, warning kinds). Research `04`
§3.4.4, traps R04.19. Closed before writing.
Evidence: none.

What it does (own words): before the classic chat starts, two checks run.
1. **First run.** If no provider is usable, print a short "not set up yet — run `indus setup`"
   block. Without a terminal, add the non-interactive guidance and exit 1. On a terminal ask "Run
   setup now? [Y/n]"; no, Ctrl+C or EOF → exit 1; yes runs the wizard, and the chat goes on only
   when a provider is usable afterwards. "Usable" is any of: the configured route resolves with a
   real credential; any provider key found in the secret store, `.env` or the environment;
   a local base URL (`model.base_url` on a loopback host, or `OPENAI_BASE_URL` / a local
   server's URL variable); a provider recorded as signed in in `auth.json`. Placeholder-shaped
   secrets (bare `sk-`, "your-key", …) never count (P03's `is_placeholder`).
2. **Expensive / data-training route.** Only when `-m` or `--provider` was given explicitly:
   collect warnings from a guard registry. On a terminal ask "Use it for this run? [y/N]"; no →
   exit 1. Without a terminal print the warnings to stderr and refuse (exit 1), except that
   data-policy warnings alone are allowed when
   `security.allow_data_training_tiers_noninteractive: true` (printed, then the run goes on).
   `--yolo` never skips it (it approves commands, not spend).

Inputs → outputs: scope (config, secrets, env, `auth.json`), the explicit flags, TTY state →
`None` (go on) or an exit code; lines on stdout (first run) / stderr (route guard).

Files: `src/indus/cli/first_run.py`, `tests/unit/cli/test_first_run.py`; shared:
`src/indus/cli/chat.py` (call both guards before the REPL / finite run),
`src/indus/foundation/config/schema.py`
(`security.allow_data_training_tiers_noninteractive`).

Edge cases / failure modes:
- a broken `auth.json` or unreadable `.env` never crashes the guard (treated as no signal);
- a test or driver that injects a route (`HostOverrides.route`) skips the first-run check;
- the guard never resolves the network; it only reads files and the env view;
- a guard function that raises is skipped (never blocks chat);
- non-TTY with both kinds of warning and the flag set: data-policy lines printed, cost refused.

Our design differences / decisions:
- P03 profiles carry no pricing or data-training metadata yet, so the built-in rule table
  (`FLAGGED_ROUTES`) is empty; guards are a registry (`register_guard`) that P08 fills from its
  pricing data and the profile fields it adds. The confirm/refuse logic is complete and tested
  with registered guards.
- After a "yes" the chat continues when setup produced a usable provider (the reference stops
  after setup and asks the user to start again).
- "Signed in" reads `auth.json` metadata directly (`providers.<id>` non-empty or an `oauth` pool
  entry); it does not import P07-06's store, so the guard stays cheap and cannot fail on a lock.

Tests we will write:
- one test per usable-provider signal, and the placeholder key that does not count;
- non-TTY → guidance + exit 1; TTY yes → setup runs; no / EOF / Ctrl+C → exit 1;
- guard table: no flags → silent; flagged + TTY yes/no; non-TTY refuse; data-policy allowed by
  the setting; cost still refused with the setting; independent of `--yolo`;
- chat wiring: the guard runs before session selection; injected route skips it.

## Decisions for the lead
- Built-in flagged-route table empty until P08 adds pricing / data-policy metadata to profiles.
- Chat continues after a successful first-run setup (one place in `chat.py`).

Compare checklist (filled after the rewrite; reopened `hermes_cli/main.py`,
`hermes_cli/model_selection_guards.py`):
- [x] env keys of every key-based provider count; `OPENAI_BASE_URL` alone counts (tests:
  `tests/unit/cli/test_first_run.py::test_usable_signals`)
- [x] `.env` file keys count (test: `tests/unit/cli/test_first_run.py::test_usable_signals`)
- [x] signed-in provider in `auth.json` counts (test:
  `tests/unit/cli/test_first_run.py::test_usable_signals`)
- [x] config route with provider/base_url counts (test:
  `tests/unit/cli/test_first_run.py::test_usable_signals`)
- [x] placeholder key ignored (test: `tests/unit/cli/test_first_run.py::test_placeholder_key_ignored`)
- [x] non-TTY prints guidance and exits 1; EOF/Ctrl+C mean no (tests:
  `tests/unit/cli/test_first_run.py::test_non_tty_exits_1_with_guidance`,
  `tests/unit/cli/test_first_run.py::test_tty_answers`)
- [x] route guard only for explicit `-m`/`--provider`; model taken from config when only
  `--provider` given; nothing to check without a model (test:
  `tests/unit/cli/test_first_run.py::test_route_guard_table`)
- [x] non-TTY data-policy allowance prints why it proceeds; cost still refused (test:
  `tests/unit/cli/test_first_run.py::test_data_policy_setting_for_unattended_runs`)
- [x] chat runs both guards before any session is opened (tests:
  `tests/unit/cli/test_first_run.py::test_chat_runs_the_first_run_guard`,
  `tests/unit/cli/test_first_run.py::test_chat_runs_the_route_guard`)
- [x] guard errors swallowed (test: `tests/unit/cli/test_first_run.py::test_failing_guard_is_skipped`)
- Deliberate differences: no Claude Code / gh credential borrowing as a signal (P2/D-16.11); the
  "explicitly configured model" gate is not needed because we never borrow foreign credentials.
  Without a terminal the "not set up" block and the guidance go to stderr (the reference prints
  them on stdout), so a script's stdout stays empty when nothing ran.
- Not taken: context-cache switch guard (live-session only; belongs to `/model`, P08).

## Status

Done (2026-10-18), with the empty rule table noted above. Shared files: `cli/chat.py`,
`foundation/config/schema.py`.

## Integration (lead)

- "Signed in" now reads through `AuthStore.signed_in()` (lazy import; any `AuthError` → no signal),
  so the rule lives in one place and a named profile also sees the root's sign-ins.
- P08 item: `FLAGGED_ROUTES` stays empty until profiles carry pricing / data-policy metadata.
