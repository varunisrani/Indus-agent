# Task: per-platform setup helpers, `indus gateway setup` (P16-31) Phase: 16 Owner: wave-27 builder E11

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/gateway_setup_wizard.py`
(skim: the platform menu with a status per platform, per-platform prompts, the allowlist and
"unauthorized users" questions, the Telegram automatic-bot offer, the service offer at the end);
per-platform setup file names only: `plugins/platforms/signal/setup.py`,
`plugins/platforms/discord/onboarding.py`, `plugins/platforms/google_chat/setup_files.py`,
`plugins/platforms/qqbot/onboard.py`, `plugins/platforms/whatsapp_cloud/setup_wizard.py`.
Evidence: none. Our own sources read: `cli/setup/` (P07 wizard, P21-13 backend credentials into the
secret store), `cli/prompts.py`, the registry, `gateway/config.py`, `gateway/core/authz.py`
(`unknown_dm_mode`, which says it exists for setup and doctor), each adapter's `create_adapter`
(which env names are credentials, which settings are config keys), `cli/commands/profile.py` (R04.26
clone stripping).

What it does (own words): `indus gateway setup [PLATFORM]` walks through one messaging platform, or
offers a menu of every registered platform with its state (on / not set up / library missing) until
"Done". For a platform it: prints what to create on the vendor's side (Slack: an app manifest to paste);
checks the platform's library (`check_fn`, with the install hint when missing); asks each value the
adapter needs — secrets with a masked prompt, stored in the profile's `SecretStore` (keychain or the
owner-only file) and never written to `.env`, config or the screen; plain settings (homeserver, IMAP
host, server URL, public webhook URL) written to `platforms.<name>` through the round-trip config
writer; asks who may talk to the bot (`allow_from`) and how unknown senders are treated
(pair / ignore / decline), then prints the mode that will apply and why (the gateway's own
`Authorizer.unknown_dm_mode`; email ignores unknown senders unless told otherwise, R06.8; broadcast
surfaces never get codes, R06.24); offers an optional home channel, checked with the P16-32 target
rules; and, when the platform is now on and its library is present, runs a connection check: the
adapter is built from the registry exactly as `indus send` builds it (send-only) and connected and
closed. Telegram then prints the `https://t.me/<bot>` link to open a chat with the bot.

A value that is already stored is kept unless the person asks to replace it. Nothing is saved for a
platform when a required value is missing, and nothing when the bot identity the answers make (the
values that key the gateway's token lock: `lock_env`, else the credentials) is already used by another
profile on this machine (its secret store or `.env`): one bot token serves one profile (R04.26, R06.16)
— a cloned profile must get its own bot.

Without a terminal it asks nothing: it prints, for the platform (or all of them), the secret names to
put in the secret store or the environment and the config keys to set, and exits 2 (R04.19).

Inputs → outputs: `indus gateway setup [PLATFORM]` → prompts; secret store entries
(`<VENDOR_NAME>`), `platforms.<name>.<key>` settings, `platforms.<name>.extra.allow_from`,
`platforms.<name>.extra.unauthorized_dm_behavior`, `platforms.<name>.home_channel`; exit 0 done,
1 cancelled, 2 no terminal / unknown platform.

Files: `src/indus/cli/wizards/__init__.py`, `src/indus/cli/wizards/gateway_setup.py`,
`tests/unit/cli/wizards/__init__.py`, `tests/unit/cli/wizards/test_gateway_setup.py`; shared:
`src/indus/cli/commands/gateway.py` (one lazy `setup` row), `tests/contract/cli/non_tty_table.yaml`
(`gateway setup: 2`).

Edge cases / failure modes:
- no terminal → guidance + 2, nothing read or written; Ctrl+C → "Gateway setup cancelled." + 1, values
  saved by finished platforms stay.
- unknown platform name → the list of names, 2; `api_server` / `webhook` → pointer to
  `indus gateway api-key` / `indus webhook` (nothing to ask here).
- secret already stored → "Replace?" (no by default); set only in the environment → said so, the
  environment value is never copied into the store.
- secret that is blank → kept (stored) or missing (required → nothing saved, the platform stays off).
- connection check failure → the adapter's message with every value just entered masked; never fatal.
- library missing → install hint; values can still be saved; no connection check.
- home channel that does not fit the platform's target rule → asked again with the rule's message.
- other profile's store unreadable (keychain locked) → that profile is skipped, not fatal.

Our design differences / decisions:
- One registry-driven wizard with a small per-platform table (questions, which value is a secret,
  which is a config key) instead of a setup function per plugin.
- Every secret goes to the secret store (the reference writes `.env`).
- The access question offers pair / ignore / decline and prints the gateway's own verdict; open access
  ("anyone") is not offered by the wizard (an allowlist of `*` stays possible by hand).
- No service install offer at the end (that is `indus gateway install`); the wizard says so.
- No Telegram managed-bot QR flow and no terminal QR codes: no QR library is a dependency, so the
  Telegram step prints the deep link only. The WhatsApp bridge pairing QR lands with the bridge
  (P16-02/03, not landed).
- The CLI reaches the gateway (registry, settings, authorizer, adapter builder, target rules) by module
  name at run time, like `indus gateway run` and `indus send` are wired (layer rule D-16.246: CLI and
  gateway are sibling top layers, no static import).

Tests we will write: a scripted run per registered platform through a fake registry entry whose
`check_fn` is a fake and whose adapter is a fake (secrets in the store only — never in config, `.env`
or the printed lines; config keys written; the connection check called; access line printed); the
menu loop; replace / keep an existing secret; required value missing → nothing saved; R04.26 another
profile with the same token → refused, nothing saved; missing library → hint, no check; Slack manifest
printed; Telegram link printed; home channel validated; non-TTY → key list and exit 2 (one platform
and all); cancel → 1; the click command through `indus gateway setup --help` and the non-TTY table.

## Decisions for the lead
- `indus setup` → "Messaging gateway" section still says "arrives in a later release" (P07 file, not
  changed): proposed to call `gateway_setup.run_menu(...)` there or point at `indus gateway setup`.
- R04.26 is enforced at setup time by comparing bot identities across live profiles; the clone itself
  (`profile.CHANNEL_PLATFORMS`) still lists only the P15-era platforms (no `line`, `ntfy`, `irc`,
  `teams`, `google_chat`, `simplex`, `buzz`, `msgraph`, `whatsapp_cloud` prefix `WHATSAPP_CLOUD_` is
  covered by `whatsapp`): proposed to derive it from `BUILTIN_CREDENTIALS` / `BUILTIN_PUBLIC_ENV` (P07
  file, not changed).
- Email's IMAP host is written to `platforms.email.imap_host`; the email token-lock key reads
  `EMAIL_IMAP_HOST` only from env/secret store (known E4/E5 seam), so a profile set up by the wizard
  locks on the address alone until `lock_key` also reads config.
- Terminal QR codes (Telegram deep link, WhatsApp pairing) need a QR library (e.g. `segno`, BSD-3):
  not added (no task names it).

Compare checklist (filled after the rewrite; reopened `hermes_cli/gateway_setup_wizard.py`):
- [x] menu of every platform with a configured/not configured state, "Done" ends it (test:
  `test_gateway_setup.py::test_menu_runs_until_done`)
- [x] already configured → reconfigure asked per value (replace an existing secret; test:
  `::test_an_existing_secret_is_kept_unless_replaced`)
- [x] required token skipped → platform not set up (`::test_a_missing_required_value_saves_nothing`)
- [x] allowlist or, without one, how unknown users are handled (`::test_every_platform_runs_with_a_fake_check`)
- [x] Slack manifest / Telegram link specials (`::test_slack_prints_a_manifest`,
  `::test_telegram_prints_the_bot_link`)
- [x] library missing → install hint (`::test_a_missing_library_prints_the_hint_and_skips_the_check`)
- [x] Telegram: the first allowed id offered as the home channel (asked yes/no) — gap found at
  compare, added (`::test_telegram_offers_the_first_allowed_person_as_home`,
  `::test_the_suggested_home_can_be_declined`)
- Deliberate differences: secrets in the secret store, never `.env` (the reference saves env values);
  the access verdict comes from the gateway's authorizer, not from the wizard's own copy; no "open
  access" choice; Discord id clean-up of `<@id>` / `user:` prefixes kept (allowlist entries are
  normalised for every platform: `<@…>` and spaces removed).
- Not taken: the Telegram managed-bot QR flow (needs the vendor's managed-bot API and a QR library),
  the service install/start/restart offer (separate `indus gateway install`), Windows/WSL/Termux
  service notes, curses menu colouring.

## Status

Done (2026-10-20). Shared: one lazy `setup` row in `cli/commands/gateway.py`, one non-TTY table row.
Proposals above for the lead (P07 setup section, clone channel list, email lock key, QR library).
