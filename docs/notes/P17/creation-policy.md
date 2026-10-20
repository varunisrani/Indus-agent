# Task: Frozen creation policy (P17-09) Phase: 17 Owner: wave-25 builder K3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/gateway_chat.py` (the
frozen-field list, the refused-flag table, the env launch flags, the bypass-launch rule, the resume
policy check and its message). Research 04 §3.4.6 and trap R04.1. Closed before writing.
Evidence: none.

What it does (own words): when the daemon creates a session it writes down, once, how that session
was launched: working directory, model, provider, reasoning effort, toolsets, the turn cap,
preloaded skills, checkpoints, auto-approve (yolo), accepting hooks, ignoring rules files, the base
URL and *which* key to use (a reference such as an env var or secret name — never the key), safe
mode, ignoring user config and passing the session id to the model. Those values belong to the session, not to the daemon, and do not
change for the session's life. A later resume may repeat any of them with the same value (scripts
re-run one command line), but a different value is refused with `policy_conflict` naming the field
(R04.1). A launch that ignores the user's config (safe mode or ignore-user-config) reads no profile
defaults, so it must name both the model and the provider. The launch flags that a person can also
set through environment variables (`INDUS_YOLO_MODE`, `INDUS_IGNORE_RULES`, `INDUS_SAFE_MODE`,
`INDUS_IGNORE_USER_CONFIG`, `INDUS_ACCEPT_HOOKS`) are read on the client side into the session's
creation request and cleared from the environment used to reach or start the daemon, so they never
leak into the daemon (and so into every other session).

Inputs → outputs:
- `CreationPolicy.from_launch(mapping)` → a validated policy (types checked, lists normalised and
  de-duplicated, an `api_key` dropped, a credential-shaped key reference or a base URL with user
  info refused `policy_refused`); `record()` / `from_record()` round trip the JSON kept in the
  session's `model_config["launch"]` (the same key the in-process CLI uses).
- `check_launch(policy)` → raises `PolicyRefused` (`bypass_needs_route`, naming the missing field).
- `check_resume(frozen, requested)` → None, or `PolicyConflict(fields)` (raised by `require_same`):
  each field the resume names whose value differs from the frozen one.
- `launch_flags_from_env(env)` → `{field: True}` for each truthy launch env var;
  `scrub_launch_env(env)` → a copy without them.
- `stored_policy(row)` / `policy_patch(policy)` → read / write helpers over `model_config`.

Files: `src/indus/service/creation_policy.py`, `tests/unit/service/test_creation_policy.py`;
shared: `src/indus/foundation/errors.py` (two `runtime.policy_*` codes),
`src/indus/service/daemon_host.py` (checks on create / resume).

Edge cases / failure modes:
- `max_turns=0` is a value, not "absent"; `False` for a switch means "not asked for";
- toolsets / skills given as a comma string or a list, with blanks and duplicates;
- cwd compared after resolving (`/a/./b` equals `/a/b`); a relative cwd is refused;
- a resume naming nothing → accepted; naming a field the session never froze → conflict (the
  explicit value differs from "the configured default");
- an API key in the launch mapping is never stored nor compared; a key reference that looks like a
  credential, or a base URL carrying `user:pass@`, is refused before anything is stored;
- unknown fields in a launch mapping are refused (`invalid_field`) so a typo is not frozen silently;
  unknown fields in a stored record are ignored (forward compatible).

Our design differences / decisions:
- One frozen record shared with the in-process CLI (`model_config["launch"]`), so a session
  created by `indus --local` and resumed through the daemon is checked against the same values.
- `key_ref` instead of an API key; worktree stays CLI-only (P17-18 refuses it in attach mode).
- The bypass rule needs model **and** provider (the reference only asks for the model).
- Model aliases are compared as typed; resolving an alias to the frozen id is left to the CLI
  attach task (P17-18) which knows the alias table.

Tests we will write:
- resume with the same model OK; a different model → `policy_conflict{field: model}`; several
  differing fields listed in order; cwd normalised; lists compared as sets;
- ignore-config / safe-mode launch without provider (or model) refused;
- secrets: an `api_key` never appears in `record()` or the stored JSON; a credential-shaped
  `key_ref` and a base URL with user info refused; the refusal text does not echo the value;
- env: truthy launch vars become flags; scrub removes exactly them; other vars kept;
- record round trip; unknown launch field refused; unknown record field ignored.

## Decisions for the lead
- Error codes `runtime.policy_conflict` and `runtime.policy_refused` added to `ERROR_CODES`
  (P17-25 maps `policy_conflict` onto the protocol table).
- The protocol's `SessionCreateParams` is not extended here (P17-25); the host takes the policy
  as a Python argument and the RPC layer (P17-13/17) builds it.

Compare checklist (filled after the rewrite; reopened `hermes_cli/gateway_chat.py`):
- [x] every frozen field is handled (test: `test_creation_policy.py::test_every_frozen_field_conflicts`)
- [x] gap found: `pass_session_id` is frozen in the reference — added to `FROZEN_FIELDS`, pinned
  by the test above and `test_the_cli_launch_record_reads_as_a_policy`
- [x] repeated values on resume are accepted (test: `test_creation_policy.py::test_resume_with_same_values_is_accepted`)
- [x] `max_turns=0` is a value (test: `test_creation_policy.py::test_max_turns_zero_is_a_value`)
- [x] API key never durable nor compared (test: `test_creation_policy.py::test_api_key_is_never_stored`)
- [x] env launch flags ride the creation request and are cleared (tests: `test_launch_env_vars_become_creation_flags`, `test_scrub_removes_only_launch_vars`)
- [x] bypass launch must name the route (test: `test_a_bypass_launch_must_name_model_and_provider`)
- Deliberate differences: bypass needs model **and** provider; unknown launch fields are refused;
  credential-shaped key references and base URLs with user info are refused; a field never
  frozen counts as "default", so an explicit value on resume conflicts (the reference compares
  against an empty record the same way).
- Not taken: alias resolution of `-m <alias>` against the frozen resolved id (P17-18); the
  refused-flag table and hints (P17-18); `client_env` forwarding (P17-18/24); freezing `source`
  (the host keeps the source in memory; a resume names its surface).

## Status

Done (2026-10-20). Shared edits: `foundation/errors.py` (`runtime.policy_conflict`,
`runtime.policy_refused`), `docs/internals/traps-ledger.md` (R04.1 rows). The host checks the
policy on create and resume (P17-08). Applying the frozen fields beyond cwd / model / toolsets to
the agent is the daemon's agent factory's job (it receives the record in the session row).
