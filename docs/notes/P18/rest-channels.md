# Task: REST: channels, webhooks, pairing, daemon control (P18-15) Phase: 18 Owner: wave-30 builder R3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_routers/messaging.py`
(route list; the per-platform payload: fields with set/unset and redacted preview, enabled /
configured / state ladder, the explicit-disable rule, the update body validated as a whole before
anything is written, blank keeps, redacted preview refused; the test answer ladder),
`hermes_cli/web_routers/ops.py` (pairing list/approve/revoke/clear-pending; webhook list/create
with the secret shown once, delete, per-route enable; gateway start/stop), `hermes_cli/web_routers/
actions.py` (restart, the drain route's body and answers). F1's client and notes from
`w29-f1.patch` (`docs/notes/P18/rest-client.md`, `lib/api.ts`: families `/api/messaging`,
`/api/webhooks`, `/api/pairing`, `/api/daemon`). Our `gateway/config.py` (credential names,
enablement, R06.15), `gateway/registry.py` (entries, policy env names), `state/pairing.py`,
`state/webhook_subscriptions.py`, `gateway/http/webhook_rules.py` (route fields, `enabled`),
`cli/commands/{webhook,pairing,gateway}.py` and `cli/wizards/gateway_setup.py` (rules only),
`service/gateway_control.py`, `service/gateway_ensure.py`, `gateway/control_socket.py` (verbs),
`gateway/daemon/side_services.py` (the `drain` verb), W1's `auth/service_bearer.py`. Reference
closed before writing. Evidence: none.

What it does (own words): the backend of the Channels, Webhooks and Pairing pages and of the
daemon buttons on the System page, for the request's profile (`read_scope` / `write_scope`).

- **Channels** (`/api/messaging/platforms`): one row per platform the gateway knows (its registry,
  reached by module name because `web` and `gateway` are sibling layers), with its label, whether
  its library is installed, `enabled` (the gateway's own enablement rule: credentials present and
  not switched off; an explicit `enabled: false` always wins, R06.15), `configured` (every
  credential present), a `state` (`disabled`, `not_configured`, `daemon_stopped`,
  `pending_restart`, or what the running daemon reports: `connected`, `retrying`, `fatal`,
  `paused`), and its fields. A field is a credential (kept in the profile's secret store), a
  setting the platform needs (`.env`) or a policy setting (allowed users, allow-all, home channel,
  in `.env`). Each field says whether it is set and, for credentials, only a redacted preview;
  plain settings show their value. `PUT …/{platform}` takes `{enabled?, env?, clear_env?}`: the
  whole body is checked first (only that platform's names, no redacted preview sent back as a
  value), then cleared names go, non-blank values are written (blank keeps the stored value) and
  `enabled` goes to `platforms.<name>.enabled` through the config writer. Nothing echoes a value.
  `POST …/{platform}/test` answers `{ok, state, message}` from that ladder.
- **Pairing** (`/api/pairing`): pending requests and approved users per platform (never a code;
  the store keeps hashes only); approve `{platform, code}` or `{platform, request_id}` (404 for
  an unknown or expired code, 429 while the platform is locked out); revoke `{platform, user_id}`;
  clear pending `{platform?}`.
- **Webhooks** (`/api/webhooks`): the receiver's on/off and base URL, and every route (subscribed
  routes and config routes) with URL, auth, events, delivery, `enabled` and `secret_set`, never a
  secret. Create `{name, events?, prompt?, deliver?, deliver_only?, auth?, description?}` makes a
  new random secret, stores it in the secret store under the route's generated name and returns
  it once in the create answer; a later list never has it. Delete drops the route and its
  generated secret. `PUT /api/webhooks/{name}/enabled {enabled}` flips the route's flag; the
  receiver re-reads the file on each request, so a disabled route answers 403 at once.
- **Daemon** (`/api/daemon`): `GET` = the state from the control socket (`identify`; a well-formed
  answer is liveness, R06.18). `POST start|stop|restart` run as background actions (`actions.py`,
  shared with P18-16) and answer 202 with the action name; the System page tails
  `/api/actions/{name}/status`. Start uses the attach ensure (`service.gateway_ensure`), stop asks
  the daemon to stop itself through its control socket (owner-only verb `stop`), restart is stop
  then ensure. `POST /api/daemon/drain {action: drain|cancel}` is W1's service-bearer route: it
  sends the daemon's `drain` verb (`{on: true|false}`) and echoes the answer.

Inputs → outputs: JSON → JSON; errors `{detail: {code, message}}`; reads `read_scope`, writes
`write_scope` (`?profile=` on a multi-profile host), except the drain route (see decisions).

Files: `src/indus/web/routes/{messaging,webhooks,daemon}.py`, `tests/contract/web/
test_messaging_routes.py`, `tests/contract/web/rest_app.py` (the shared builder), `tests/contract/web/fake_control.py`,
`docs/notes/P18/rest-channels.md`. Shared: `web/routes/__init__.py` ROUTERS (+3), traps R06.15.

Edge cases / failure modes:
- A credential sent as its own redacted preview (`ab…yz`) → 400, nothing written.
- One bad name in `env` or `clear_env` → 400 and nothing at all is written (whole-body check).
- `enabled: false` with credentials in the env/secret store → stays `disabled` (R06.15).
- Unknown platform → 404; gateway registry not importable → empty list (never 500).
- Secret store unreadable → the field reads unset; a write failure is 500 with no value echoed.
- Webhook name invalid → 422-free 400 `bad_name`; existing name → 409; config routes cannot be
  deleted or toggled here (409 `config_route`).
- Daemon not running: stop → action fails "not running"; drain → 409 `daemon_not_running`.
- The daemon does not know the `stop` verb (today's daemon) → the action ends with a plain
  message naming `indus gateway stop`; nothing signals a pid.
- An action already running under the same name → 409 `action_running`.

Our design differences / decisions:
- Credentials go to the secret store (as `indus gateway setup` does), settings to `.env`; the
  reference writes every value to `.env`.
- State names are ours (`daemon_stopped` for the reference's gateway-stopped); no WhatsApp/Telegram
  QR onboarding flows, no multiplexer checks, no auth-posture ladder (later, if needed).
- Daemon routes never signal a pid: the control socket for stop/drain, the ensure for start.
- No `system`-scope service installs exist here (our units are per user and `--system` only
  prints), so nothing asks for sudo; the row's "fail fast without passwordless sudo" cannot arise.

Tests we will write (`tests/contract/web/test_messaging_routes.py`):
- route list pinned to `ROUTE_LIST` of each module; every write depends on `write_scope` except
  the drain route.
- channels: list shape with no value; blank keeps old value; redacted preview refused; bad name →
  nothing written; `enabled: false` with an env token stays disabled; clear removes; test ladder.
- pairing: approve by code and by request id, wrong code 404, revoke, clear pending.
- webhooks: secret returned once then absent from list; disable → receiver flag false; delete
  removes the generated secret; duplicate 409.
- daemon: a fake control socket (real Unix socket server in the test) answers `identify` and
  `stop`: restart action reported in the action status (stop verb seen, then ensure called);
  stop when the daemon refuses the verb → failed action with the CLI hint; drain sends `drain`
  `{on}` and echoes; drain while stopped → 409.

## Decisions for the lead
- Daemon stop/restart need an owner-only control verb `stop` (`params {reason, restart}`) in the
  daemon's side services (record a planned stop with the reason, then begin the normal stop). Not
  in this slice (gateway files are not ours): until it lands the routes report that the daemon does
  not take a remote stop and name `indus gateway stop`.
- The drain route resolves the backend's own profile unless `?profile=` names one (a service
  bearer caller does not know the profile list); it is the one unscoped write here.
- The gateway registry and settings are reached by module name (`importlib`), like the CLI's
  setup wizard; a public descriptor API in `service` would be cleaner.
- Pairing routes live in `messaging.py` (the row names no file for them).
- An unsuffixed `GET /api/daemon` (state) is added for the restart banner (P18-25).

Compare checklist (filled after the rewrite; reopened `messaging.py` update/test, `ops.py`
pairing/webhooks/gateway, `actions.py` drain):
- [x] whole body validated before any write; blank keeps; redacted preview refused (tests:
  `tests/contract/web/test_messaging_routes.py::test_one_bad_name_writes_nothing`,
  `::test_blank_keeps_the_stored_value_and_settings_show_their_value`,
  `::test_a_masked_preview_sent_back_is_refused`)
- [x] explicit disable wins over credentials (R06.15) (test: `::test_explicit_disable_beats_an_env_credential`)
- [x] list shows set/unset + masked preview, allowlist-style settings show their value (test:
  `::test_platforms_list_fields_and_never_a_credential`)
- [x] test answer ladder: disabled → missing setup → daemon stopped → connected (tests:
  `::test_test_ladder_names_what_is_missing_and_unknown_platform_is_404`,
  `::test_live_state_comes_from_the_daemon`)
- [x] pairing: approve by code or request id, lockout only on the code path (our store), 404 for an
  unknown code, revoke 404 when not approved, clear pending count (tests:
  `::test_pairing_list_approve_revoke_and_clear`, `::test_approve_by_request_id_and_bad_platform`)
- [x] webhook secret surfaced once on create, `secret_set` on reads, per-route enable kept on disk and
  honoured by the receiver's route model (tests: `::test_webhook_secret_is_shown_once_then_never`,
  `::test_webhook_disable_reaches_the_route_file_and_delete_drops_the_secret`,
  `::test_disabled_route_is_refused_by_the_receiver_rules`)
- [x] restart in the background; drain begin/cancel echo (tests:
  `::test_restart_goes_through_the_control_socket_then_ensure`, `::test_drain_and_cancel_use_the_drain_verb`,
  `::test_drain_accepts_the_service_bearer`)
- Deliberate differences: credentials go to the secret store; restart/stop through the control
  socket (never a spawned CLI that signals pids); webhook creation is allowed while the receiver is
  off (the list shows `receiver_enabled`, and `PUT /api/webhooks/receiver` turns it on) and never
  takes a caller-chosen secret; the receiver toggle does not restart the daemon (answers
  `restart_needed`); per-platform env value rules and the auth-posture ladder are not taken.
- Not taken: WhatsApp/Telegram QR onboarding, multiplexer refusals and migrate routes, gateway
  `migrate/plan`.

## Status

Done (2026-10-20). Shared: `web/routes/__init__.py` ROUTERS (+3 of 7 lines), traps-ledger R06.15 and
R09.20 rows. Owed elsewhere: the daemon's owner-only `stop` control verb (see Decisions); W1 test
pins listed in the report.

Integration (P18 REST integrator, 2026-10-20): the daemon now registers the owner-only `stop`
verb (`gateway/daemon/side_services.py` `DaemonBridge._stop`, `control_socket.KNOWN_VERBS` /
`OWNER_ONLY_VERBS`): it writes the planned-stop record `indus gateway stop` writes (reason
`restart` when `restart: true`, so the next attach starts it again; any other stop stays stopped)
and runs the normal shutdown. The dashboard's stop and restart work end to end against a real
in-process daemon (`tests/integration/web/test_daemon_stop.py`); the "does not take a remote
stop" message stays for an older daemon (`test_messaging_routes.py`, fake control without
`stop`). Builders merged: `rest_app.py`, `fake_control.py`. Decisions numbered D-16.597 (verb),
D-16.618 (daemon routes), D-16.619 (channels, pairing, webhooks).
