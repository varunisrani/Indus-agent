# Task: `config set/unset` routing and coercion (P07-12) Phase: 07 Owner: P07 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config.py`
(`set_config_value`, `unset_config_value`, `_is_env_config_key`, `_validate_config_key`,
`_is_wrong_prefix_suggestion`, `_run_write_command`, usage tables). Research `04-cli-config.md`
§3.13.3–3.13.6, §3.14.2; traps R04.7, R04.8, R04.9, R04.12. Closed before writing.
Evidence: none.

What it does (own words): `indus config set KEY VALUE` decides where a value belongs, turns the
typed text into the right type and writes it through the one writer for that place.

Routing, first match wins:
1. managed install → refused (nothing can be changed here);
2. an empty key or empty segment (`a..b`, `a.`, ` a`) → refused;
3. a bare name (no dot) that looks like a credential (ends `_API_KEY`, `_TOKEN`, `_SECRET`, or is a
   key name a bundled provider profile reads) → the profile's secret store (the OS keychain when it
   works), stored under the upper-case name; when the user picked `security.secret_store: file` it
   goes to `.env` instead. A stale copy of the same name in `.env` is removed so the old value
   cannot come back;
4. any other UPPER_SNAKE name → `.env` through P01's writer (its denylist refuses `LD_PRELOAD`,
   `PATH`, git helpers, Indus policy switches…, R04.12). UPPER_SNAKE names never reach
   `config.yaml`, `--force` or not;
5. everything else → `config.yaml` through the P01 writer: `api_base`/`model.api_base` are saved as
   `model.base_url` with a note; the key is checked against the schema (a key that is a known key
   with an extra prefix is refused with a did-you-mean unless `--force`; other unknown paths are
   written and followed by a notice, with a did-you-mean when one is close); the value is coerced by
   the type of the slot's default (P01 `keys.coerce`: string slots keep the literal, so
   `approvals.mode off` stays `"off"`; true/false/null words, ints and floats otherwise;
   list/mapping-looking text is parsed as YAML and refused when malformed); P01 `keys.check_write`
   refuses a scalar over a section without `--force` and a scalar in a list slot always (R04.7);
   P01 `keys.set_path` prefers an existing literal key with dots and refuses a phantom sibling
   (R04.8). Changing `model.provider` drops `model.base_url`/`model.api_mode` that belonged to the old
   provider and says what was dropped, keeping a local URL, a URL on the new provider's own host or
   any URL when the new provider is a custom one (R04.9).

`indus config unset KEY`: credential names are removed from the secret store and from `.env`;
other UPPER_SNAKE names from `.env`; anything else is removed from `config.yaml` (empty parents go
too). Nothing removed → `Config key not set: KEY`, exit 1. Every refusal prints to stderr, says
nothing was written and exits 1.

Inputs → outputs: `config set KEY VALUE [--force]`, `config unset KEY` → one confirmation line on
stdout (credential values masked), notices on stdout, refusals on stderr with exit 1.

Files: `src/indus/cli/commands/config_write.py`, `src/indus/cli/config_coerce.py` (routing,
key checks, masking helpers shared with the read side), `tests/unit/cli/commands/test_config_write.py`.

Edge cases / failure modes:
- A config file that cannot be read or parsed → refused before anything is computed (the loader's
  failed read, and the writer's own fail-closed re-read).
- `model: some-id` shorthand in the file and `set model.provider x` → the shorthand becomes
  `{default: some-id, provider: x}` (the loader canonicalises; the writer keeps the result).
- `set model gpt-x` over a `model:` mapping → redirected to `model.default` by P01's guard.
- Lower-case credential names (`openai_api_key`) are stored upper-case.
- A denied env name (`LD_PRELOAD`, `INDUS_YOLO_MODE`) is refused on both the `.env` and the
  credential route.
- Values with non-ASCII characters going to `.env` keep the writer's warning.

Our design differences / decisions:
- Credentials go to the keychain (P4, D-16.13) rather than `.env`; `.env` only on explicit file
  storage.
- Admin-pinned keys: the admin overlay (I20) is not built, so there is nothing to refuse yet; the
  check is a stub in `config_coerce.admin_pinned` that P01's overlay can fill.
- Env-seeded credential-pool entries: the pool model (P07-07, slice B) is not in this tree. Removing
  the key is all `unset` does; if slice B stores env-seeded entries rather than deriving them, the
  lead wires its prune call into `unset_credential`.
- No mirroring of `terminal.*` into `.env` (our env loader bridges `terminal.*` itself).

Tests we will write:
- coercion table through the command (string slot `off`, ints, floats, bools, null, list literal,
  malformed list refused, scalar into list slot refused, scalar over section refused / `--force`);
  dotted keys (`providers.p.models.grok-4.6.x` lands on the literal key; phantom sibling refused;
  `\.` escape creates one); credential routing (keychain fake, file mode → `.env`, stale `.env`
  copy removed); UPPER_SNAKE → `.env`, never YAML with `--force`; `LD_PRELOAD` refused; empty
  segments refused; managed install refused; wrong-prefix key refused, `--force` writes; unknown
  key notice; provider switch drops a stale URL and keeps a local one; `api_base` alias; comments
  preserved after set (round-trip golden); unset of each route and the not-set exit 1.

## Decisions for the lead
- See the two stubs above (admin pins, pool prune).
- Credential detection uses the bundled provider profiles' key names plus the suffix rule; named
  custom providers' generated names (`INDUS_CUSTOM_<SLUG>_API_KEY`) are covered by the suffix rule.

Compare checklist (filled after the rewrite; reopened `hermes_cli/config.py` set/unset parts):
- [x] managed refused; empty key and empty segments refused (tests:
  `test_config_write.py::test_managed_install_refused`, `::test_bad_keys_refused`)
- [x] credential-shaped bare names to the secret side, other UPPER_SNAKE to `.env`, never YAML even
  with `--force` (tests: `::test_credential_goes_to_keychain`, `::test_upper_snake_goes_to_env_file`)
- [x] env writer denylist on every path (test: `::test_denied_env_names_refused`)
- [x] wrong-prefix refusal unless `--force`, unknown path notice (tests:
  `::test_wrong_prefix_refused_unless_forced`, `::test_unknown_key_written_with_notice`)
- [x] coercion by default type, malformed structured refused (test: `::test_coercion_table`)
- [x] scalar model shorthand before `model.*` writes (test: `::test_model_shorthand_kept_with_provider`)
- [x] section and container guards (test: `::test_section_and_container_guards`)
- [x] dotted literal keys, phantom sibling refused (test: `::test_dotted_keys`)
- [x] provider switch drops the old route and says so (test: `::test_provider_switch_drops_stale_url`)
- [x] `api_base` alias (test: `::test_api_base_alias`)
- [x] unset: not set → exit 1 (test: `::test_unset_routes_and_missing`)
- [x] comments, order and quoting survive a set (test: `::test_set_keeps_comments_round_trip`)
- [x] unreadable file refused untouched (test: `::test_unreadable_file_refused`)
- Deliberate differences: keychain instead of `.env` for credentials; masked echo uses
  `redact.mask`; the provider-switch check keeps any URL when the new provider is a custom one.
- Not taken: per-platform display-key redirection, `display.language` locale check (locales P21),
  skin file touch, terminal env mirroring, admin pins (not built).

## Status

Done (2026-10-18).

## Integration (lead)

- Pool prune wired: after `config unset NAME` removes a credential, `pool_model.prune_env_rows`
  drops NAME's stored env-seeded pool rows (slice B materialises them on pool writes).
