# Task: `browser_vault` model-blind autofill (P24-40) Phase: 24 Owner: wave-38 builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/browser_vault_tool.py` (the
five actions, origin pre-check and in-script re-check, payment confirmation, redaction
registration, result shapes), `hermes_cli/vault.py` (add/list/rm/sources), and for the vendor CLI
shapes only `agent/vault_backends/onepassword.py`, `agent/vault_backends/bitwarden.py` (grep of the
argv lines), `agent/vault_store.py` (grep: Fernet, function names); research 02 §3.25 vault.
Closed before writing. Evidence: none.

Vendor interfaces pinned: 1Password CLI **v2** (`op item list --categories Login --format json`,
`op item get <id> [--vault <v>] --fields label=password --reveal`, `op item get <id> --otp`,
`op signin --raw` with the master password on stdin; session token in `OP_SESSION`); Bitwarden CLI
(`bw list items --nointeraction`, login items are `type == 1`; `bw get password <id>`, `bw get totp
<id>`, `bw unlock --raw --nointeraction --passwordenv <VAR>`; session token in `BW_SESSION`). Fernet
(`cryptography` 50.0.2) for the local vault.

What it does (own words): the agent can use saved logins, cards and addresses on the current page
without ever seeing them. `list` returns handles and non-secret metadata (label, kind, bound
origin, login identifier — the agent types identifiers itself) and names locked managers.
`fill <handle>` resolves the secret locally and writes it into the page only when the page's
origin exactly equals one of the item's bound origins: checked before, and checked again inside
the fill script itself, so a redirect between the two writes nothing. Login items fill the
password field; payment items fill card fields only after the user approves an approval card;
address items fill address fields. `save_login` asks the user (masked prompts) for the identifier
and password of the current page, stores them in the local vault bound to that origin and fills
the password. `enter_code` fills a one-time code: from the manager's TOTP seed when it has one,
else asked of the user. `unlock` asks the user for a manager's master password (1Password,
Bitwarden) or, for the local vault, approval to open it for this process. Tool results carry only
handles, counts, origins and outcomes; every filled value is added to the session's redaction set,
so a later snapshot showing a filled field's value is masked; a test scans every result and log.
`indus vault add | list | remove | lock | unlock` manages the local vault; values are read hidden,
never from argv.

Inputs → outputs: tool `browser_vault {action, handle?, backend?, label?}` → JSON results; local
vault file `<home>/vault/local.vault` (Fernet token, 0600) with its key in the profile's
`SecretStore` as `INDUS_VAULT_KEY` (OS keychain when it works); `<home>/vault/locked` marks it
locked; `browser.vault_sources` lists the backends to use.

Files: `src/indus/tools/builtin/browser/vault.py`, `src/indus/cli/commands/vault.py`, tests
`tests/unit/tools/builtin/browser/test_vault.py`, `tests/unit/cli/commands/test_vault.py`; shared:
`cli/app.py` (`vault` row), contract help catalogue + non-TTY table, schema row
`browser.vault_sources`, `tests/fakes/browser.py` (an `eval_hook` for scripted pages).

Edge cases / failure modes:
- `https://bank.example` item on `https://bank.example.evil` → `origin_mismatch`, nothing written;
- the page navigates between the pre-check and the write → `origin_changed`, nothing written;
- payment without approval (denied, no broker, unsupported) → `payment_declined`, nothing written;
- manager locked → listed under `locked`; `fill` asks to unlock first; no prompt possible
  (headless) → `unlock_unavailable`;
- no page open / `about:blank` → error, nothing asked; no matching field → error;
- an exception from the page evaluation never surfaces its text (it can hold the script with the
  secret): a fixed message, scrubbed of every secret value;
- secrets never in argv: `op` gets the master password on stdin, `bw` through an env var of its own
  child only; session tokens live in memory per profile and process, passed by env.

Our design differences / decisions:
- Injection runs through the session's page driver (Playwright's own channel), never a CLI argv.
- Local-vault "lock" is a marker the CLI sets; the agent's `unlock` of the local vault needs the
  user's approval card and lasts for the process.
- No login-control classifier port: a small field classifier of our own (type, autocomplete, name,
  id, placeholder hints).

Tests we will write:
- origin mismatch, redirect between checks, payment without approval, payment with approval;
- list shows no secrets and reports locked managers; save_login and enter_code flows with a fake
  broker; op/bw argv shapes with a fake runner (master password on stdin / env, never argv);
- every result and the caplog scanned for every secret substring; a later snapshot masks a filled
  value; CLI add/list/remove/lock/unlock with hidden prompts and the non-TTY rule.

## Decisions for the lead
- Extra `vault = ["cryptography==50.0.2"]` (already locked; Apache-2.0 OR BSD-3-Clause).
- `browser_vault` is registered by `register_vault_tool(registry, tools)`; it joins `BROWSER_TOOLS`
  only when the lead decides (golden tool arrays).

Compare checklist (filled after the rewrite; reopened the files above):
- [x] origin exact, checked twice (tests: `test_vault.py::test_origin_mismatch_refuses_fill`, `::test_redirect_between_checks_refuses`)
- [x] payment needs confirmation (tests: `::test_payment_fill_without_approval_is_refused`, `::test_payment_fill_with_approval_fills_card_fields`)
- [x] list carries identifiers but never secrets; locked managers listed (test: `::test_list_shows_metadata_only_and_locked_managers`)
- [x] values registered for redaction (test: `::test_filled_value_is_masked_in_a_later_snapshot`)
- [x] stop on declined save / code (tests: `::test_save_login_declined`, `::test_enter_code_asks_the_user_without_a_seed`)
- Deliberate differences: no supervisor socket (the page driver is ours); no tab focusing across tabs.
- Not taken: `vault sources --enable/--disable` (config row instead), address/OTP classifier depth,
  desktop settings pages.

## Status

Done (2026-10-21).

## Integration (P24 integrator, 2026-10-21)

Wired: `browser_vault` registers over the browser tools' session manager and is in `BROWSER_TOOLS`
(golden arrays regenerated); the `vault` extra names cryptography.
