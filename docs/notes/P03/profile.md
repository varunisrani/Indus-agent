# Task: Provider profile (P03-01) Phase: 03 Owner: P03-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `providers/base.py` (field list and
hook names), `providers/README.md` (hook table), `website/docs/developer-guide/adding-providers.md`
(fast path, pitfalls). Closed before writing.
Evidence: none.

What it does (own words): one frozen record per model provider that says how to reach it — name
and aliases, wire format (`api_mode`), how it authenticates, which env names hold its key (in
priority order), its default endpoint and the host that endpoint lives on, optional default
headers, and a few request quirks (which max-tokens field it wants, a fixed temperature, a
context-window hint, model aliases). Three hooks are plain callables with no-op defaults:
`prepare_body(body, model)`, `classify_error(ctx)` and `context_length(model)`. A profile only
describes; clients, pools and streams live elsewhere (http, transports, streaming).

Inputs → outputs: keyword fields → a validated, hashable, immutable `ProviderProfile`, or
`InvalidProviderProfile` (code `provider.invalid_profile`) naming the bad field.

Files: `src/indus/providers/profile.py`, `tests/unit/providers/test_profile.py`; shared:
`src/indus/foundation/errors.py` (provider error classes and codes).

Edge cases / failure modes:
- `api_key` auth without a base URL is refused (nowhere to send the key); `api_key_optional`
  and `none` may leave it empty (the bare `custom` profile takes its URL from config).
- Base URL must parse under the P03-04 rules (http/https, no userinfo, no whitespace).
- Any default header whose value carries another product's token (`Name/1.2`) is refused, and a
  `User-Agent` must be ours (`indus/<version>`) — R01.12. CR/LF in header names or values refused.
- Env names must match `^[A-Z][A-Z0-9_]*$`; names lowercase `[a-z0-9][a-z0-9._-]*`.
- `hostname` is derived from `base_url` (lowercased, IDNA, no trailing dot) unless set.
- Mapping fields are frozen copies so a caller cannot mutate a shared profile.

Our design differences / decisions:
- Plain frozen dataclass with hook callables as fields instead of subclass overrides.
- `api_mode` accepts `chat_completions`, `anthropic_messages` now and the P08 names
  (`responses`, `bedrock`, `gemini`) so later profiles validate; routes fall back until P08.
- No OAuth auth types (P07), no external-process providers, no account-usage, aux-model, vision
  default or SDK-client hooks (later phases add them when a consumer exists).
- Extra fields for the parallel P03 transports and timeouts: `family` (`vendor`/`custom`),
  `auto_detect`, `reasoning_efforts`, `reasoning_models`, `adaptive_thinking_models`,
  `bearer_auth` (see Decisions).

Tests we will write: minimal valid profile; hostname derived/normalised; explicit hostname kept;
`api_key` without URL refused; optional/none without URL allowed; foreign `User-Agent` and a
foreign product token in another header refused; our UA accepted; CR/LF refused; bad env names
and names refused; unknown api_mode/auth_type/max_tokens_param refused; frozen (assignment and
mapping mutation fail); hooks default to no-ops; hashable; custom hooks called.

## Decisions for the lead
- Fields added beyond the task row so P03-09/10/13 have somewhere to read their flags:
  `reasoning_efforts` (supported ladder levels, `None` = unknown), `reasoning_models` (substring
  patterns for the reasoning stale floor), `adaptive_thinking_models`, `bearer_auth` (Anthropic-wire
  hosts that take `Authorization: Bearer`), `family`, `auto_detect`. `reasoning_efforts`,
  `adaptive_thinking_models` and `bearer_auth` follow the lead's ruling for the transports slice
  (an earlier draft had `auth_header`; replaced). Transports read `route.profile` and
  `route.api_key.reveal()`.
- `classify_error` is typed `Callable[[object], object | None]` because the `Verdict` type lives
  in P03-15 (other slice); tighten after landing.

Compare checklist (filled after the rewrite; reopened `providers/base.py`, `providers/README.md`):
- [x] identity, auth, endpoint, models URL, hostname, headers, temperature, max tokens, hidden,
  model aliases covered (test: `tests/unit/providers/test_profile.py::test_minimal_profile_defaults`)
- [x] hostname derived from base URL (test: `::test_hostname_derived_and_normalised`)
- [x] hooks default to no-ops (test: `::test_hooks_default_to_no_ops`)
- [x] foreign identity headers refused (tests: `::test_foreign_user_agent_refused`,
  `::test_foreign_product_token_in_any_header_refused`)
- Deliberate differences: frozen record with callable fields, not subclass hooks; constructor
  validation (the reference validates nothing); our UA only.
- Not taken (phase that needs them): `signup_url`, `fallback_models` (P07 picker),
  `supports_health_check` (P07 doctor), `auth_handler`/`refresh_credential` (P07 OAuth),
  `supports_vision_tool_messages`, `supports_prompt_cache_key`, `native_reasoning_details_type`,
  extra-body / api-kwargs hooks, `default_reasoning_config` (P08), external-process fields
  (not planned), account usage (P08), aux model (P08), `create_client` (never: no SDKs),
  `fetch_models` on the profile (lives in `model_list`, P03-17).

## Status

Done (2026-10-18). Shared change: provider error classes in `foundation/errors.py`.

## Integration (lead)

- `foundation/errors.py` merged with P02 store and P03-B classes/codes (union, conflict only).
