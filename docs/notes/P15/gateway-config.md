# Task: Gateway config section, platform config and enablement (P15-02) Phase: 15 Owner: wave-18 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/config.py` (`PlatformConfig`,
`HomeChannel`, `ChannelOverride`, streaming defaults, the platform enum), `gateway/platform_env.py`
(enablement steps), `gateway/config_env.py` (`_enable_from_env` and the explicit-disable warning),
`gateway/config_loader.py` (skimmed: what it merges and what it does not),
`hermes_cli/session_reset_retirement.py` (which legacy keys are looked at). Research 06 §3.2, R06.15,
R06.25; D-16.22, D-16.32. Evidence: none.

What it does (own words): the gateway reads its settings through the one P01 loader and defaults
table: `platforms.<name>.*` and `gateway.*` are rows of the shared schema, so a value set only in
defaults behaves exactly as in the CLI (the reference's gateway read raw YAML and diverged). A
platform starts when its vendor credentials are present (secret store first, environment second):
`TELEGRAM_BOT_TOKEN`; `DISCORD_BOT_TOKEN`; `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`. An explicit
`enabled: false` in config always wins over present credentials and logs exactly one warning per
platform per profile per process, naming the ignored credential names (never values). A missing
`enabled` key keeps credential enablement; `enabled: true` without credentials does not start the
platform and is reported as a problem. Policy env names use our prefix
(`INDUS_<PLATFORM>_ALLOWED_USERS`, `_ALLOW_ALL_USERS`, `_ALLOW_BOTS`, `_HOME_CHANNEL`,
`INDUS_GATEWAY_ALLOWED_USERS`, `INDUS_GATEWAY_ALLOW_ALL_USERS`). The legacy idle/daily conversation
reset block (`session_reset` at the top level or under `gateway`) is never read; a timed one gives
one startup warning and the same line for `doctor`.

Inputs → outputs: scope (config view, secrets, env) → `GatewaySettings` { per-platform
`PlatformSettings` (effective enabled, reason, config section, credentials, home channel), gateway
section, problems, warnings }.

Files: `src/indus/gateway/config.py`, `tests/unit/gateway/__init__.py`,
`tests/unit/gateway/test_config.py`; shared: `src/indus/foundation/config/schema.py` (+ its test).

## Schema rows added (shared file `foundation/config/schema.py`)
- `platforms`: `PlatformsSection` with `telegram`, `discord`, `slack` (each a `PlatformSection`);
  other names (plugin platforms, P16) are kept as extra keys and validated by `gateway.config`.
- `PlatformSection`: `enabled: bool | None = None` (None = follow credentials),
  `home_channel: HomeChannelSetting | None = None` (`platform`, `chat_id`, `name`, `thread_id`),
  `reply_to_mode: off|first|all = first`, `restart_notice: bool = True`,
  `typing_indicator: bool = True`, `channel_overrides: {id: {model?, provider?, system_prompt?}} = {}`,
  `extra: {} `.
- `gateway`: `GatewaySection` with `message_timestamps.enabled: false` (P15-23 reads it). Later tasks
  add their own `gateway.*` rows to this class (G2: `bot_loop_guard`, `max_concurrent_sessions`,
  `trust_env`, `group_sessions_per_user`, ...).
- `display.busy_ack: true`, `display.busy_text_quiet_s: 0.35`, `display.busy_text_cap_s: 1.0`
  (P15-12 reads them; the files-to-update row puts them under P15-02).

## API
`indus.gateway.config`:
- `BUILTIN_CREDENTIALS: Mapping[str, tuple[str, ...]]` (telegram, discord, slack).
- `policy_env(platform) -> PolicyEnvNames(allowed_users, allow_all_users, allow_bots, home_channel)`;
  `GLOBAL_ALLOWED_USERS_ENV`, `GLOBAL_ALLOW_ALL_ENV`.
- `read_secret(scope, name) -> str | None` (secret store, then env; blank = absent).
- `@dataclass(frozen=True) class HomeChannel(platform, chat_id, name="", thread_id=None)`.
- `@dataclass(frozen=True) class PlatformSettings`: `name`, `enabled`, `reason`, `section:
  PlatformSection`, `credentials: Mapping[str, str]` (hidden from `repr`), `home_channel`,
  `explicit: bool | None` (the config value). Shortcuts: `reply_to_mode`, `restart_notice`,
  `typing_indicator`, `channel_override(chat_id)`, `extra`.
- `@dataclass(frozen=True) class GatewaySettings`: `platforms: Mapping[str, PlatformSettings]`,
  `gateway: GatewaySection`, `problems: tuple[str, ...]`, `enabled() -> list[str]` (sorted).
- `load_gateway_settings(scope, *, credentials=BUILTIN_CREDENTIALS) -> GatewaySettings`.
- `legacy_reset_notice(tree) -> str | None` (doctor line), `reset_warnings()` (tests).

Edge cases / failure modes:
- blank or whitespace credential = absent; one of Slack's two tokens missing = absent.
- `enabled: "false"` string in YAML: Pydantic coerces; an invalid value → treated as unset, one
  problem line.
- a platform block that fails validation → that platform falls back to defaults plus a problem
  line; other platforms unaffected.
- plugin platform blocks with no credential table → enabled only by `enabled: true`.
- the warning set is keyed by (home, platform) so two loads warn once, and two profiles each warn.
- `INDUS_<P>_HOME_CHANNEL` used only when config has no `home_channel`.

Our design differences / decisions:
- No gateway-only loader or defaults (R06.25); no `gateway.json` legacy layer; no YAML→env bridge
  (the reference copies YAML into process env; we read config and secrets per scope).
- No `token`/`api_key` keys in platform config: credentials only from the secret store or env.
- `restart_notice` (our name for the restart notification switch).
- Config `home_channel` wins over the env var (the more specific source); the env value is a chat id
  only (threads through config).
- `enabled: true` with no credentials does not start the platform (reported as a problem).

Tests we will write:
- table over credentials (present/absent) × enabled (true/false/missing) incl. Slack's two tokens.
- warning exactly once per platform across two loads (caplog), names not values.
- secret store wins over env.
- round trip through `config.save` + reload; default-only key equals CLI view value
  (shared-loader test: `config.load(scope).get(...)` and the gateway section agree).
- legacy `session_reset` notice: timed modes warn once, `none` silent.
- schema pins updated (`test_schema.py`).

## Decisions for the lead
- Config home channel beats env; env holds a chat id only.
- `enabled: true` without credentials → not started, `problems` entry.
- `gateway.message_timestamps.enabled` lives under `gateway` (the P15-23 row says
  `message_timestamps.enabled`; relative to the gateway section).
- display busy keys named `busy_ack`, `busy_text_quiet_s`, `busy_text_cap_s` (our names).

Compare checklist (filled after the rewrite; reopened `gateway/config.py`, `gateway/config_env.py`):
- [x] env × enabled table for the three platforms incl. Slack's two tokens (`tests/unit/gateway/test_config.py::test_enablement_table`, `::test_slack_needs_both_tokens`)
- [x] one warning per platform across two loads, names not values (`::test_explicit_disable_warns_once_per_platform_names_not_values`, `::test_no_warning_when_disabled_without_credentials`)
- [x] secret store before env (`::test_secret_store_comes_before_env`); credentials never in `repr` (`::test_settings_repr_never_shows_credentials`)
- [x] defaults from the shared table, same as the CLI view (`::test_defaults_come_from_the_shared_table`) — R06.25
- [x] round trip through the P01 writer, idempotent re-save (`::test_settings_round_trip_through_the_writer`)
- [x] legacy `session_reset` (top level and under `gateway`) not read, warned once (`::test_legacy_reset_block_is_not_read_and_warns_once`)
- [x] gap found at compare: settings written straight under `platforms.<name>` were not reachable through `extra` — fixed (`PlatformSettings.extra` merges them, the `extra` block wins), pinned by `::test_settings_under_the_platform_block_reach_extra`
- Deliberate differences: no `token`/`api_key` in config; no gateway-only loader, no `gateway.json` layer, no YAML→process-env bridge; `enabled: true` without credentials does not start; config home channel beats the env var; warnings keyed per profile home.
- Not taken (owners): `typing_status_text` (P15-35 if Slack needs it), Discord link → channel id normalisation for home channels (P15-34), streaming defaults (P15-16), session-key settings, unknown-DM behaviour, bot-loop and concurrency settings (G2: P15-07, P15-08, P15-10 add their `gateway.*` rows to `GatewaySection`).

## Status

Done (2026-10-19). `src/indus/gateway/config.py`, `tests/unit/gateway/{__init__,test_config}.py`; shared: `src/indus/foundation/config/schema.py` (new `HomeChannelSetting`, `ChannelOverride`, `PlatformSection`, `PlatformsSection`, `MessageTimestampsSection`, `GatewaySection`; `ConfigModel.platforms`, `ConfigModel.gateway`; `display.busy_ack`, `busy_text_quiet_s`, `busy_text_cap_s`) and `tests/unit/foundation/config/test_schema.py` (pins). Doctor wiring of `legacy_reset_notice` is left to the lead (cli and gateway are independent layers; the doctor check registry lives in cli).
