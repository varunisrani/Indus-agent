# Task: REST: config, env keys, effective config (P18-12) Phase: 18 Owner: wave-30 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_routers/config_env.py`
(route list; config get/put deep-merge with a strict read before write; defaults and schema; env
list rows with redacted preview, description, url, category, password and advanced flags; put and
delete through the credential lifecycle; the denylist error → 400; the preview-as-value refusal).
F1's client (`w29-f1.patch`: `apps/web/src/lib/{api,restTypes}.ts`, `testing/mockApi.ts`, notes
`rest-client.md`, `page-config-keys.md`). Research 09 §3.2/§3.4, traps R04.12, R04.15,
16-what-we-change §2.6 item 3. Our `foundation/config/{loader,writer,schema,keys}.py`,
`foundation/{secrets,envfile,envload,redact,home}.py`, `cli/commands/config_write.py` and
`cli/config_coerce.py` (routing and secret-name rules; not importable from `web`), `providers/bundled.py`.
Reference closed before writing. Evidence: none.

What it does (own words): the Config and Keys pages' backend.

- Config: `GET /api/config` is the profile's settings as the form edits them: the defaults with the
  user's file laid over, **not** env-expanded (a `${NAME}` stays `${NAME}`, so no secret reaches the
  page through a reference), internal `_…` keys left out, and any literal value under a
  credential-shaped key (`api_key`, `token`, `password_hash`, …) replaced by a placeholder.
  `PUT /api/config {config}` lays the body over the file through P01's round-trip writer (comments,
  order and templates survive; values equal to defaults are not written); a placeholder sent back
  keeps the stored value. An unreadable file (I/O or YAML error) is 500 `config_unreadable` and
  nothing is written (fail closed); a managed install is 403 `config_managed`.
- `GET /api/config/defaults` and `/schema` are public (W1's allowlist) and profile-free: the schema
  lists every leaf the Pydantic models declare as `{type, description, category, options?}` with
  `type` in string / integer / number / boolean / enum / list, plus `category_order` (the model's
  section order).
- `GET /api/config/effective` → `{entries: [{key, value, source}]}` for every leaf: `default`
  (built-in), `file` (the user's file), `env` (the file names `${NAME}`; the value shown is the
  reference, never the expansion), `overlay` (admin overlay; the loader has none yet, so never
  emitted today).
- Keys: `GET /api/env` → `{NAME: {category, description, advanced, is_password, is_set,
  redacted_value, url}}` for every key a bundled provider reads (plus its base-URL override as an
  advanced setting) and every name already in the secret store or the profile's `.env` (category
  `custom`). `PUT /api/env {key, value}`: credential-shaped names go to the `SecretStore`
  (keychain by default; `.env` when `security.secret_store: file`), and an older `.env` copy is
  removed; other UPPER_SNAKE names go to `.env` through P01's env writer. The env writer's denylist
  (`LD_*`, `PATH`, `PYTHONPATH`, …) is checked first on both paths → 400 `env_name_denied` (R04.12).
  A blank value keeps the stored one. A value equal to the current redacted preview is refused (a
  preview must never become the key). The answer is `{key, is_set, redacted_value}`: the value is
  never echoed (R04.15). `DELETE /api/env {key}` removes it from the store and `.env`.

Inputs → outputs: see above; reads `read_scope`, writes `write_scope`; errors
`{detail: {code, message}}`.

Files: `src/indus/web/routes/config_env.py` (routes), `src/indus/web/routes/config_view.py`
(schema walk, redaction, effective entries, key catalog), `tests/contract/web/test_config_env_routes.py`;
shared: one line in `src/indus/web/routes/__init__.py`.

Edge cases / failure modes:
- Unreadable `config.yaml` (bad YAML or unreadable): GET/PUT/effective → 500, file bytes unchanged.
- `PUT` body not an object → 422.
- Comments in `config.yaml` survive a PUT (round-trip writer).
- `LD_PRELOAD`, `ld_preload`, `PATH` → 400; lower-case or dotted names → 400 `invalid_key`.
- Secret store errors → 503 `secret_store_unavailable` (message without the value).
- Value with CR/LF or non-ASCII: the env writer cleans it (its warning is not echoed with the value).
- Secret in a response: scanned by tests on every route's body.

Our design differences / decisions:
- No "reveal" route (R04.15; F1's page has none).
- Signup links: our provider catalog has none, so `url` is null until one exists.
- DELETE of a key that is not set answers 200 `{removed: false}` (idempotent) instead of 404.
- Config GET redacts literal credentials found in `config.yaml` (placeholder `••••••••`), and PUT
  restores them; the reference returns the file as written.

Tests we will write: PUT round-trips comments; defaults stripped; `${NAME}` kept and not expanded in
GET; placeholder kept on PUT; unreadable file → 500 and no write (bytes and mtime unchanged);
schema has enum/boolean/integer/string fields and categories; effective marks a `${NAME}` key as
`env`, a file key as `file`, others `default`; `LD_PRELOAD` refused; credential goes to the secret
store and leaves `.env`; plain setting goes to `.env`; blank keeps; preview refused; secret absent
from every response body; delete; multi-profile write without `profile` → 400; every write route
depends on `write_scope`; the route list constant equals the app's routes.

Route list (`{method, path, scoped}`; `ROUTE_LIST` in `routes/config_env.py`, pinned by a test):

```json
[
  {"method": "GET", "path": "/api/config", "scoped": true},
  {"method": "PUT", "path": "/api/config", "scoped": true},
  {"method": "GET", "path": "/api/config/defaults", "scoped": false},
  {"method": "GET", "path": "/api/config/schema", "scoped": false},
  {"method": "GET", "path": "/api/config/effective", "scoped": true},
  {"method": "GET", "path": "/api/env", "scoped": true},
  {"method": "PUT", "path": "/api/env", "scoped": true},
  {"method": "DELETE", "path": "/api/env", "scoped": true}
]
```

## Decisions for the lead
- `config/defaults` and `config/schema` take no profile (they are public and static); F1's stub
  marked schema `scoped: true` — harmless (the client adds `?profile=` to the whole `/api/config`
  family), but the generated list says `false`.
- The `env` source means "expanded from a `${NAME}` reference"; other env overrides (e.g.
  `INDUS_DASHBOARD_PUBLIC_URL`) are applied by their consumers, not the loader, so the view cannot
  see them.
- Credential routing mirrors `indus config set` (suffix `_API_KEY`/`_TOKEN`/`_SECRET`/`_PASSWORD`
  or a bundled provider key name → secret store). The CLI's helpers live in `cli/` (layer rule),
  so the rule is restated here; moving it to `foundation/config/keys.py` would let both share it.
- `is_set` counts the secret store and the profile's `.env` only, not the dashboard process env.

Compare checklist (filled after the rewrite; reopened `hermes_cli/web_routers/config_env.py`):
- [x] config GET drops `_` internals; PUT lays the body over the file (never replaces it) after a
  strict read (tests: `tests/contract/web/test_config_env_routes.py::test_put_round_trips_comments_and_writes_only_changes`,
  `::test_unreadable_config_is_500_and_nothing_is_written`)
- [x] schema `{fields, category_order}`, defaults (test: `::test_schema_and_defaults_are_public`)
- [x] env rows: set/unset, redacted preview, description, url, category, password, advanced;
  custom names from the file listed as passwords (tests: `::test_credential_goes_to_the_secret_store_and_is_never_echoed`,
  `::test_plain_settings_go_to_the_env_file`)
- [x] denylisted names → 400 with the reason (test: `::test_denied_and_malformed_names_are_refused`)
- [x] a redacted preview sent as a value is refused (test: `::test_credential_goes_to_the_secret_store_and_is_never_echoed`)
- [x] delete clears every copy (gap closed: the stored key and an older `.env` line both go)
  (test: `::test_delete_removes_from_store_and_env_file`)
- [x] `${NAME}` never expanded into a response; literal credentials in the file hidden and kept on
  save (ours; test: `::test_references_stay_references_and_credentials_stay_hidden`)
- Deliberate differences: credentials go to the keychain-backed `SecretStore` by default
  (D-16.13) instead of `.env`; DELETE of an unset key is 200 `removed: false`, not 404; config GET
  hides literal credentials; the effective view is ours (16 §2.6 item 3).
- Not taken: reveal route (R04.15), custom-endpoint and provider-validation routes, egress status,
  live credential probes, `include_defaults` / `preserve_language` switches, dynamic provider
  options in the schema, channel-managed and provider-grouping row fields.

## Status

Done (2026-10-20). Shared: `src/indus/web/routes/__init__.py` (one ROUTERS line), traps
R04.12/R04.15 lines, ACC-L5-1 lines. Same `install(...)` wiring as P18-11.

Integration (P18 REST integrator, 2026-10-20): the generated route list says `config/schema`
and `config/defaults` are unscoped, and F1's client now agrees (`UNSCOPED_PATHS` in
`apps/web/src/lib/api.ts`: no `?profile=` on them). Decisions numbered D-16.611.
