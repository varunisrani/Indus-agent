# Task: `indus auth` commands (P07-08) Phase: 07 Owner: P07 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/auth_commands.py`
(command bodies, list row shape, exhausted-status wording, interactive menu outline),
`hermes_cli/subcommands/auth.py` (flags); research `04-cli-config.md` §3.16.3, §3.16.8.
Closed before writing.
Evidence: none.

What it does (own words): the command line for credential pools. `add` stores a key (masked
prompt when not given, never echoed) or runs a provider's documented sign-in flow, `list` shows each
pool with index, label, type, source, status and time left on a cooldown, `remove`/`reset`/
`priority` edit a pool, `refresh` renews an OAuth entry's tokens, `status` summarises one provider,
`logout` forgets everything stored for a provider and clears `model.provider` when it named it.
Bare `auth` in a terminal opens a small menu (add, remove, reset, strategy); without a terminal it
prints the pools and how to use the subcommands. There is no `indus login` and no portal
`upgrade`: `indus login` reaches the unknown-command answer, which points at `indus auth add`.

Inputs → outputs:
- `add PROVIDER [--type oauth|api-key] [--label L] [--priority N] [--api-key K] [--no-browser]
  [--timeout S] [--ca-bundle PEM] [--insecure]` → "Added <p> credential #n (label)"; unknown
  provider → exit 1 with close names; non-TTY without `--api-key` → exit 1 with guidance; OAuth
  asked for a provider without a documented flow → exit 1.
- `list [PROVIDER]`, `remove P T`, `reset P [T]`, `priority P T N`, `refresh P [T]`, `status P`,
  `logout P` → plain lines; misses → exit 1 naming the provider.

Files: `src/indus/cli/commands/auth.py`, `tests/unit/cli/commands/test_auth.py`; new
`src/indus/providers/auth/flows.py` (the registry of documented flows); shared:
`src/indus/cli/app.py` (one REGISTRY row), `src/indus/cli/unknown.py` (`MOVED` rows for `login` and
`logout`), `src/indus/cli/commands/__init__.py` (created by slice A too; docstring only).

Edge cases / failure modes:
- The key is written to the SecretStore first; if that fails no pool row is written.
- Removing an env entry cannot delete the variable: it is hidden (suppressed) and the person is
  told how to remove it (`indus config unset NAME`).
- `refresh` with several OAuth entries and no target → exit 1 asking for one.
- `logout` of a provider that has env keys says those still apply.

Our design differences / decisions:
- D-16.80: no `login`/`logout` top-level commands and no `upgrade`.
- OAuth is the default type only for a provider whose flow entry says so; OpenRouter's PKCE key
  mint is offered with `--type oauth` but keys stay the default (P2).
- Prompts: until P07-01's prompt kit lands, the masked prompt and menu use click's prompt
  (`hide_input=True`); the lead swaps them to `cli.prompts` at integration (two helper functions).

Tests we will write:
- `CliRunner` with a fake SecretStore for every subcommand; non-TTY add without `--api-key` exits
  1; logout resets `model.provider`; unknown provider hint; `indus login` hint; the secret never in
  `auth.json` or in output.

## Decisions for the lead
- Bare `auth` without a TTY prints the pools plus a usage line and exits 0 (informational) for the
  P07-26 table.

Compare checklist (filled after the rewrite; reopened both reference files):
- [x] Every subcommand and flag present (test: `test_auth.py`)
- [x] logout resets `model.provider` only when it names this provider (tests: `test_auth.py::test_logout_clears_state_and_resets_model_provider`, `::test_logout_keeps_another_provider_setting`)
- [x] remove of an env entry hides it until the next add (test: `test_auth.py::test_removing_an_env_entry_hides_it_until_the_next_add`)
- [x] `indus login` → hint (test: `test_auth.py::test_login_is_not_a_command_and_points_at_auth_add`)
- Deliberate differences: see above.
- Not taken: plugin auth dispatch, external login adoption notices, Codex same-account warning,
  provider-specific status blocks (Bedrock/Azure), portal free-tier lines.

## Status

Done (2026-10-18). Shared changes: `src/indus/cli/app.py` (one REGISTRY row: `auth`), `src/indus/cli/unknown.py` (`MOVED` rows for `login`/`logout`), `src/indus/cli/commands/__init__.py` (docstring only; slice A creates the same file). Prompts use click until P07-01 lands (`_ask_secret`, `_ask_text`, `_ask_choice` in `commands/auth.py`).

## Integration (lead)

- The click prompts are gone: `_ask_secret` / `_ask_text` / `_ask_choice` call `cli.prompts`
  (masked line, arrow list) on the `Interactive` from `prompts.io_for(state, AUTH_GUIDANCE)`;
  `NonInteractive` prints that guidance and exits 1, Ctrl+C exits 1. "Terminal" now means stdin
  and stdout both (was stdin only). Tests script answers through `prompts.io_factory`.
- `indus status` / `indus dump` list the providers signed in here (`AuthStore.signed_in()`).
