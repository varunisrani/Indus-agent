# Task: Child environment builder (P05-05) Phase: 05 Owner: wave-1 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/local_env_policy.py`
(whole: blocklist sources, force prefix, AWS carve-out, case folding, dynamic internal names,
profile gates, venv markers), `tools/env_passthrough.py` (whole), `tools/code_execution_env.py`
(whole: safe-prefix scrub), `website/docs/user-guide/security.md` (§Environment Variable
Passthrough, §MCP Credential Handling). Test names and the first fixtures of
`tests/tools/test_local_env_blocklist.py`; the force-prefix and profile-passthrough cases in it.
Research `02-tools.md` §3.14, trap R02.22. Closed before writing.
Evidence: none.

What it does (own words): one function decides what environment a child process gets. Every
spawn (terminal commands, background processes, the bang shell, quick commands; later code
execution, MCP stdio servers, the browser driver, delegated children) asks it, so a model-authored
command never sees the provider keys the agent itself uses, and a child spawned for profile B never
carries profile A's home or secrets. Without it, `dict(os.environ)` at a spawn site leaks every key
the launch profile loaded into whatever the model runs (R02.22).

Inputs → outputs: `child_env(scope, *, variant="default", extra=None, session_id=None)
→ dict[str, str]`.
- `default` (terminal and friends): start from `scope.env` (the scope's view of the process env,
  never `os.environ`), merge `extra`, pull out `_INDUS_FORCE_<NAME>` entries, drop every blocked
  name, add passthrough names, re-add forced names, pin `INDUS_HOME`, `INDUS_SESSION_ID`,
  `AI_AGENT=indus-agent`, `INDUS_AGENT=true`.
- `minimal` (MCP stdio): only PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR from the scope env,
  then `extra` as given (the server's configured values, the user's explicit choice).
- `code` (execute_code later): names that contain none of the credential words (KEY, TOKEN,
  SECRET, PASSWORD, CREDENTIAL, PASSWD, AUTH, DSN, WEBHOOK, CREDS, BEARER, APIKEY, COOKIE) and start
  with a safe prefix (PATH, HOME, USER, LANG, LC_, TERM, TMPDIR, TMP, TEMP, SHELL, LOGNAME, XDG_,
  PYTHONPATH, VIRTUAL_ENV, CONDA); then passthrough, forced names and the pins.
- Blocklist = our static floor (provider keys/base URLs, web-backend keys, platform tokens, GitHub
  tokens, sandbox vendor tokens, the Bedrock bearer token) ∪ names in the `.env` managed-key
  registry (the provider registry registers its declared key and base-URL names there) ∪
  `key_env` names of custom providers in the scope config ∪ names registered through
  `register_blocked_names(source, names)` (adapters, plugins, sandbox vendors, tests) ∪ every
  `INDUS_*` / `_INDUS_*` name (policy and session vars; the pins are set afterwards).
- `passthrough_names(scope)` → (accepted, refused): `terminal.env_passthrough` from the scope
  config plus `register_passthrough(names)`; a blocked name is refused (logged once per call site)
  and can never be re-allowed. Values come from the scope's secret store first, then the scope env;
  a missing value is omitted, never read from the process env.
- `is_blocked(name)` for other callers (redaction, MCP config checks).

Files: `src/indus/safety/child_env.py`, `tests/unit/safety/test_child_env.py`,
`tooling/checks/no_raw_environ.py` + `tooling/checks/no_raw_environ_allow.json` +
`tooling/checks/tests/test_no_raw_environ.py`; register in `tooling/checks/__init__.py` `NAMES`
(the runner reads it; shared).

Edge cases / failure modes:
- Name case: blocklist matched case-folded (`openai_api_key` is the same variable on Windows).
- The general AWS chain (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`, …) stays:
  the local terminal is the operator's own shell; only `AWS_BEARER_TOKEN_BEDROCK` (inference) goes.
- The user's own secrets that only look like ours (`MY_APP_KEY`, `SLACK_USER_TOKEN`) stay in the
  default variant; they are dropped only by `code`.
- `_INDUS_FORCE_X` given in the env or in `extra` forces `X` through with that value; the force
  names themselves never reach the child; a forced pin name loses to the pin.
- Passthrough of a blocked name: refused, warning logged, nothing forwarded.
- Two scopes in one process produce different `INDUS_HOME` (pinned from `scope.home`).
- Venv leakage: `PYTHONHOME` is always dropped; `VIRTUAL_ENV` / `CONDA_PREFIX` are dropped when they
  point at the interpreter Indus runs in (a child `uv sync` in another project must not target our
  venv); a user's own activated venv elsewhere stays.
- A broken registered source (callable raising) must not open the gate: registered sources are
  plain name sets, so nothing can raise at build time.

Our design differences / decisions:
- One function with a `variant` argument instead of several builders.
- All `INDUS_*` names are dropped from children instead of a per-name list: simpler and fails
  closed; the pins are re-added. (Change: `KEPT_PRODUCT_NAMES`.)
- Profile-gate env (platform allow-lists) is not shape-detected here; adapters register their gate
  names through `register_blocked_names` when the gateway lands.
- `minimal` keeps exactly the row's eight names (no `XDG_*`).
- No provider import: `safety` and `providers` share a layer; the provider names arrive through the
  foundation managed-key registry and the registration hook.

Tests we will write:
- every name from a fake provider registry (registered via the hook) and from the static floor is
  absent; case-folded name absent; AWS chain kept, Bedrock bearer dropped; user secrets kept.
- passthrough: value from the secret store; blocked name refused (and logged); missing value
  omitted; config list read from the scope config.
- `_INDUS_FORCE_X` honoured from env and extra; force keys never forwarded.
- two scopes → two `INDUS_HOME`; session id, agent markers.
- `minimal` exact key set + configured values; `code` drops `MY_DB_DSN`, `GITHUB_TOKEN`,
  `SERVICE_COOKIE`, keeps safe prefixes and passthrough.
- venv markers.
- check: flags a seeded `dict(os.environ)`, `os.getenv`, `from os import environ` in `src/`;
  allow-list entries need a reason and must exist.

## Decisions for the lead
- The provider registry is not imported (same layer); `child_env` reads
  `foundation.envload.managed_key_names()` (filled by `providers.bundled.register_managed_env_names`
  when the registry is built). If a host spawns children before building the registry, the static
  floor still covers the bundled names. To be stricter, call
  `register_blocked_names("providers", bundled.env_var_names())` at service start.
- `terminal.env_passthrough` is read from the scope config; P05-04 owns the `terminal.*` schema
  and should add `env_passthrough: list[str] = []`.
- Existing `os.environ` readers in `src/` are allow-listed with reasons (process bootstrap,
  pre-scope defaults, the one snapshot in `EnvView.from_process`) instead of rewritten.

Compare checklist (filled after the rewrite; reopened `tools/environments/local_env_policy.py`,
`tools/env_passthrough.py`, `tools/code_execution_env.py`):
- [x] static floor covers provider keys/base URLs, web keys, platform tokens, GitHub, sandbox
  vendors (test: `test_child_env.py::test_static_floor_is_dropped`); added HELICONE/HASS after compare
- [x] registry-declared provider names blocked (test: `test_child_env.py::test_fake_registry_names_never_reach_children`,
  `::test_managed_key_registry_is_a_source`, `::test_custom_provider_key_env_from_config_is_blocked`)
- [x] AWS chain kept, Bedrock bearer dropped (`::test_aws_chain_kept_but_bedrock_bearer_dropped`)
- [x] another tool's own login (`CLAUDE_CODE_OAUTH_TOKEN`) stays (`::test_users_own_secret_names_stay_in_default_variant`)
- [x] case-folded match (`::test_matching_ignores_case`)
- [x] force prefix from env and extra; force names stripped (`::test_force_prefix_from_env_and_extra`)
- [x] passthrough refuses blocked names, re-checked at build time; values per scope; missing omitted
  (`::test_passthrough_of_blocked_name_is_refused`, `::test_registered_passthrough_and_late_blocking`,
  `::test_passthrough_missing_value_never_read_from_process`)
- [x] code variant word list and safe prefixes (`::test_code_variant_drops_credential_words_and_unsafe_prefixes`)
- [x] venv markers (`::test_runtime_location_markers`)
- [x] guard flags a seeded `dict(os.environ)` (`tooling/checks/tests/test_no_raw_environ.py::test_flags_seeded_dict_of_environ`)
- Deliberate differences: the reference keeps a hand list of product variables (dashboard tokens,
  relay secrets, auxiliary per-task keys) and an exact-name allow-list for code children; we drop
  every `INDUS_*` name and pin the four we own. The reference refuses to force its dynamic internal
  secrets; our force prefix brings any name through (only internal callers can set it). The
  reference strips only its own `PYTHONPATH` entries; we leave `PYTHONPATH` alone.
- Not taken: shape detection of platform allow-list gates (adapters will register names through
  `register_blocked_names` in the gateway phase); the platform-specific first-party carve-out for
  terminal children; Windows-essential names in the `code` variant (Windows terminal is later);
  per-home user-plugin manifest scanning (plugins register through the hook instead).

## Status

Done (2026-10-18). `child_env`, `passthrough_names`, `register_blocked_names`,
`register_passthrough`, `is_blocked` in `src/indus/safety/child_env.py`; guard
`tooling/checks/no_raw_environ.py` registered in `tooling/checks/__init__.py` (shared) with
`no_raw_environ_allow.json` listing the eight existing process-env owners. Spawn sites (P05-01..03,
13, later MCP/browser/delegation) call `child_env(scope, variant=…, extra=…, session_id=…)`.
