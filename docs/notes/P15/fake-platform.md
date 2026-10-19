# Task: Fake platform harness and shared adapter contract suite (P15-01) Phase: 15 Owner: wave-18 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tests/gateway/` (listing only, no test
bodies), `gateway/platforms/ADDING_A_PLATFORM.md` (the per-adapter obligations list). Research 06 §3.2,
§3.15; phase interop table (vendor names). Evidence: none.

What it does (own words): an in-memory chat platform every adapter test talks to. Tests inject
inbound traffic (DM, group, thread, bot-authored, edited, voice / photo / document payloads, button
presses), read back an ordered log of every outbound operation (send, edit, delete, typing, reaction,
media, buttons) with message ids and fake-clock times, and arm failures by class (connect timeout,
read timeout, rate limit with retry-after, chat not found, thread not found, forbidden, parse error,
too long). The platform enforces its own length cap, so an adapter that does not split is caught.
Per-platform wire fakes (Telegram, Discord, Slack) sit on top and emulate only the library surface
our adapters will call, raising library-shaped exceptions. One shared contract suite runs against
every registered adapter case; P15-33..35 and P16 add their adapters to the same list. A socket
guard makes any real `connect()` fail the test. The vendor-name drift check proves that every library
name our adapters (and wire fakes) use exists on the pinned library objects; it skips per library
when the optional extra is not installed (CI installs none).

Inputs → outputs: test scripts → fake inbound records / adapter events; adapter calls → `ops` log,
message store, raised fake errors.

Files: `tests/fakes/platform/__init__.py` (grown export), `tests/fakes/platform/core.py`,
`tests/fakes/platform/telegram_wire.py`, `tests/fakes/platform/discord_wire.py`,
`tests/fakes/platform/slack_wire.py`, `tests/contract/gateway/__init__.py`,
`tests/contract/gateway/conftest.py`, `tests/contract/gateway/test_adapter_contract.py`,
`tests/contract/gateway/test_vendor_names.py`, `tests/unit/fakes/test_platform_wires.py` (new, the
fakes' own checks); `tests/unit/fakes/test_platform_backend.py` keeps passing unchanged.

## API
`tests.fakes.platform.core`:
- `FailureKind(StrEnum)`: `CONNECT_TIMEOUT, READ_TIMEOUT, RATE_LIMIT, CHAT_NOT_FOUND, THREAD_NOT_FOUND,
  FORBIDDEN, PARSE_ERROR, TOO_LONG`; `FakePlatformError(kind, text, retry_after)` and one subclass
  per kind (class names carry the reading: `FakeConnectTimeout`, `FakeReadTimeout`, ...).
- `Op` (frozen): `kind` (`send|edit|delete|typing|typing_stop|reaction|media|buttons|answer|connect|
  disconnect`), `chat_id`, `message_id`, `text`, `thread_id`, `reply_to`, `at` (fake monotonic),
  `extra`.
- `FakeMessage`: `message_id, chat_id, text, thread_id, reply_to, buttons, media, deleted, edits`.
- `FakeChatPlatform(name="fake", *, clock=None, max_len=4096, len_fn=len, bot_user_id="bot")`:
  outbound API (`async connect/disconnect/post/edit/delete/typing/typing_stop/react/upload/answer`),
  failure arming (`fail(kind, *, op="*", chat_id=None, times=1, retry_after=None)`,
  `fail_connect(kind)`), inbound builders (`dm`, `group`, `thread`, `bot_message`, `edited`,
  `voice`, `photo`, `document`, `button_press`) that queue `FakeInbound` records and notify
  subscribers (`subscribe(callback)`, `async next_inbound()`), readers (`ops`, `ops_of(kind)`,
  `messages(chat_id=None)`, `texts(chat_id=None)`, `message(id)`), `network_calls` (always 0 for
  the fake; the socket guard covers real ones).
- `FakePlatform` stays exported with its P00 API (send/edit/texts/push_inbound/receive).
`tests.fakes.platform.telegram_wire` / `discord_wire` / `slack_wire`: `TelegramWire`,
`DiscordWire`, `SlackWire` with a `SURFACE` mapping (`"module:Object"` → names) that the drift check
reads, built over one `FakeChatPlatform`.
`tests.contract.gateway.conftest`: `AdapterCase(name, build)`, `ADAPTER_CASES` (list P15-33..35 and
P16 append to), `register_case`, fixtures `socket_guard` (autouse), `fake_clock`, `platform`,
`adapter_case`.

Edge cases / failure modes:
- armed failure scoped by op and chat, consumed in order; `times` > 1; rate limit carries the wait.
- too-long text rejected by the platform's own `len_fn` (UTF-16 for Telegram's wire).
- edits to an unknown or deleted message → message-not-found error (sub-chat reading).
- inbound builders with threads / bots / edits keep ids stable and increasing.

Our design differences / decisions:
- The fake is platform-neutral; wire fakes translate to the library shapes.
- The contract suite's seeded misbehaving adapters (sends real HTTP, swallows a rate limit, re-sends
  a split payload whole) live in the suite module and are expected to fail named checks.

Tests we will write: the contract suite (connect/disconnect idempotent, send ids, split under the
length function, edit per flag, typing refresh, approvals buttons or fallback, inbound normalised,
error classification per injected failure, no real network) green for the echo adapter; each
misbehaving adapter fails its named check; socket guard raises on `connect()`; wire fakes' own unit
checks; drift check skips cleanly without extras.

## Decisions for the lead
- Wire fakes model only the names in the phase interop table; P15-33..35 extend them.
- The drift check is skip-per-library when the extra is missing (CI installs none); run it with
  `uv sync --all-extras` once the extras exist (P15-33..35 add them).

Compare checklist (filled after the rewrite):
- [x] contract suite green for the echo adapter: lifecycle, connect failure, send ids, split under the length function, no whole re-send, edit per flag, typing refresh, approvals buttons/fallback, inbound normalised, error classification per injected failure, no real network (`tests/contract/gateway/test_adapter_contract.py::test_adapter_contract`, `::test_adapter_uses_no_real_network`)
- [x] seeded misbehaving adapters fail their named check and only it (`::test_misbehaving_adapters_fail_the_named_check`, `::test_misbehaving_adapters_still_pass_the_other_checks`)
- [x] socket guard raises on `socket.connect`, `connect_ex`, `create_connection` and asyncio connects (`::test_socket_guard_raises_on_any_connect`)
- [x] drift check: wire fakes implement their declared surface (always runs); declared names exist in the installed library (skips without the extra) (`tests/contract/gateway/test_vendor_names.py`). Verified once by hand in a throwaway venv: all 23 declared objects and names exist in python-telegram-bot 22.8, discord.py 2.7.1, slack-sdk 3.45.0, slack-bolt 1.30.0.
- [x] the fake's own checks (`tests/unit/fakes/test_platform_wires.py`); the P00 recorder test still passes unchanged
- Obligations from `ADDING_A_PLATFORM.md` the suite already pins: connect/disconnect, send returning a result with an id, typing, buttons with text fallback, normalised inbound, bot-message marking. Left to the adapter tasks' own tests: self-message filtering, inbound dedupe, reconnect backoff, identifier redaction in logs, home-channel and cron wiring (P15-20), setup/status surfaces (P15-28).

## Status

Done (2026-10-19). `tests/fakes/platform/{__init__,core,telegram_wire,discord_wire,slack_wire}.py`, `tests/contract/gateway/{__init__,conftest,test_adapter_contract,test_vendor_names}.py`, `tests/unit/fakes/test_platform_wires.py`. P15-33..35 and P16: build the adapter over the wire fake, `register_case(AdapterCase(...))` in `tests/contract/gateway/conftest.py` (ids must be numeric for Telegram and Discord), declare `vendor_names`, and extend the wire's `SURFACE`/`FAKES` together.
