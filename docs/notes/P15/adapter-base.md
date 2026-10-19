# Task: Adapter base contract and capability flags (P15-05) Phase: 15 Owner: wave-18 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/base.py`
(`BasePlatformAdapter` attribute block from ~1856, `connect`, `edit_message`, `send_typing`,
`send_exec_approval`, `send_clarify` signatures only). Research 06 §3.2, R06.13, R06.24.
Evidence: none.

What it does (own words): one abstract `Adapter` every platform implements, plus a frozen
`Capabilities` record the rest of the gateway reads instead of asking "which platform is this".
Lifecycle is idempotent by construction: the base keeps the connected state and calls the adapter's
`_open` / `_close` only on a real transition. Outbound calls go through base methods that guard the
contract: every exception becomes a classified `SendResult` with secrets scrubbed from the error
text, optional operations a platform lacks are refused cleanly (never sent), and every send and edit
to one chat shares one async slot: sends wait for it, an interim edit is skipped when the slot is
busy, the final edit is never gated (R06.13); the slot also spaces operations per adapter
(Telegram 1.0 s, others 0). Inbound events go out through `emit(event)` to the admission pipeline
the runner installs. Every adapter lists the vendor library names it touches (`vendor_names`) so the
P15-01 drift check can prove they exist in the pinned library.

Inputs → outputs: platform settings (P15-02) + clock → adapter; calls → `SendResult` / `ChatInfo`;
inbound platform payloads → `emit(InboundEvent)`.

Files: `src/indus/gateway/adapters/__init__.py`, `src/indus/gateway/adapters/base.py`,
`src/indus/gateway/core/send_slot.py`, `tests/unit/gateway/adapters/__init__.py`,
`tests/unit/gateway/adapters/test_base.py`, `tests/unit/gateway/core/test_send_slot.py`.

## API (G2 and later waves build on this)

`indus.gateway.adapters.base`:
- `LenFn = Callable[[str], int]`; `code_points(text)`; `utf16_units(text)`.
- `@dataclass(frozen=True) class Capabilities`: `max_len: int = 4096`, `len_fn: LenFn = code_points`,
  `splits_natively: bool = False`, `supports_edit: bool = False`, `supports_delete: bool = False`,
  `supports_buttons: bool = False`, `owns_access_policy: bool = False`, `auth_upstream: bool = False`,
  `dm_is_private: bool = True` (False for broadcast surfaces, R06.24), `push_capable: bool = True`,
  `slot_spacing: float = 0.0`, `inline_wait_cap: float = 60.0`.
- `@dataclass(frozen=True) class ChatInfo(name: str, type: ChatType | None)`.
- `ApprovalChoice = Literal["once", "session", "always", "deny"]`;
  `@dataclass(frozen=True) class ApprovalAction(label, choice, style="")`;
  `@dataclass(frozen=True) class ApprovalPrompt(request_id, session_key, text, fallback_text,
  actions: tuple[ApprovalAction, ...])` — wording comes from P15-22; the base sends `fallback_text`
  on adapters without buttons.
- `@dataclass(frozen=True) class ClarifyPrompt(clarify_id, session_key, question, choices: tuple[str, ...],
  multi: bool, fallback_text)`.
- `approval_callback(request_id, choice) -> str` / `parse_approval_callback(data)`;
  `clarify_callback(clarify_id, index) -> str` / `parse_clarify_callback(data)` (our own `apv:` /
  `clr:` prefixes, ≤64 bytes for Telegram callback data).
- `InboundSink = Callable[[InboundEvent], Awaitable[None]]`.
- `class AdapterConnectError(Exception)`: `retryable: bool`, `code: str`.
- `class Adapter(ABC)`:
  - class attributes: `platform: ClassVar[str]`, `capabilities: ClassVar[Capabilities]`,
    `vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {}` (`"module:Object"` → attribute names).
  - `__init__(self, *, clock: Clock, secrets: Sequence[str] = (), sink: InboundSink | None = None)`.
  - state: `connected: bool`, `slots: SendSlots`.
  - lifecycle: `async connect(*, is_reconnect=False)`, `async disconnect()` → abstract
    `_open(is_reconnect)`, `_close()`; `_open` raises `AdapterConnectError` on failure.
  - outbound (concrete, guarded): `send(chat_id, text, *, reply_to=None, meta=None)` → abstract
    `_send(...)`; `edit(chat_id, message_id, text, *, final=False, meta=None)` → `_edit(...)`;
    `delete(chat_id, message_id) -> bool` → `_delete`; `typing_start(chat_id, meta=None)`,
    `typing_stop(chat_id)` (default no-op); `send_image / send_file / send_voice / send_video /
    send_document(chat_id, path, *, caption=None, meta=None)` → `_send_media(kind, ...)` (default
    refused); `send_approval(chat_id, prompt, meta=None)` (buttons via `_send_approval`, else
    `fallback_text`); `send_clarify(chat_id, prompt, meta=None)` (same); `send_notice_private(chat_id,
    user_id, text, meta=None)` (default: refused — never a public send); `chat_info(chat_id)`;
    `format(text) -> str` (default identity); `send_with_retry(chat_id, text, *, reply_to=None,
    meta=None, rng=random.random)` (P15-04 policy with this adapter's inline cap).
  - inbound: `set_sink(sink)`, `async emit(event)`.
  - `scrub(text) -> str` masks this adapter's credentials (foundation redactor) in any text.
- Meta keys the base understands: `"plain": True` (send without formatting), `"thread_id"`,
  `"interim": True`.

`indus.gateway.core.send_slot`:
- `class SendSlots(clock, spacing=0.0)`: `async with slots.hold(chat_id)` (wait, then pace);
  `async pace(chat_id)` (inside a hold: wait out the spacing, stamp); `try_interim(chat_id) ->
  AbstractAsyncContextManager | None` (None = skip); `mark(chat_id)` (final edit: no wait, stamps
  the time); `busy(chat_id) -> bool`.

Edge cases / failure modes:
- connect twice / disconnect twice / disconnect before connect: one transition each.
- `_open` raising: state stays disconnected, the error is re-raised (typed).
- `_send` raising: classified failure, token scrubbed from the error text.
- edit on a platform without edits: refused, the platform sees nothing.
- interim edit while a send waits for spacing: skipped; final edit while a send holds: goes through.
- two chats: independent slots.
- emit without a sink: dropped with one warning per adapter.
- callback data over 64 bytes refused when built.

Our design differences / decisions:
- Template methods (`send` → `_send`, ...) instead of overridable public methods, so idempotency,
  scrubbing, refusals and the slot cannot be skipped by an adapter.
- `connect` raises a typed `AdapterConnectError` instead of returning a bool (the daemon needs
  retryable vs fatal).
- `send_notice_private` default refuses instead of a public send (a "private" notice in a group would
  leak); adapters with ephemeral messages override it.
- Media send defaults refuse (the sender posts a short notice, P15-18).
- No status-text, draft-streaming, handoff-thread, slash-confirm, model-picker or TTS hooks yet:
  later tasks add them when they need them.
- Our own callback prefixes (`apv:`, `clr:`).

Tests we will write:
- base: idempotent lifecycle; failure in `_open`; exception → classified, scrubbed result;
  refusals per capability flag; approval/clarify fallback text vs buttons; private notice refused;
  emit to sink and without one; callback round trip and 64-byte cap; utf16 length; retry wiring uses
  the adapter's inline cap.
- slot (fake clock): 5 sends to one chat start 1.0 s apart; interim edit during a waiting send is
  dropped; final edit goes through at once; two chats do not block each other.

## Decisions for the lead
- `send_notice_private` refuses by default (see above).
- Callback data format `apv:<request_id>:<choice>` and `clr:<clarify_id>:<index>`.

Compare checklist (filled after the rewrite; reopened the `BasePlatformAdapter` parts):
- [x] lifecycle idempotent, typed connect failure, credentials scrubbed (`tests/unit/gateway/adapters/test_base.py::test_lifecycle_is_idempotent`, `::test_open_failure_is_typed_and_scrubbed`, `::test_send_exceptions_become_scrubbed_classified_results`)
- [x] optional operations refused without reaching the platform (`::test_optional_operations_are_refused_without_reaching_the_platform`)
- [x] approvals / clarify: buttons or text fallback (`::test_approval_and_clarify_fall_back_to_text_without_buttons`, `::test_buttons_used_when_supported`)
- [x] shared slot (R06.13): 5 sends 1.0 s apart, interim dropped while a send waits, final not gated, chats independent (`tests/unit/gateway/core/test_send_slot.py`, `::test_edits_share_the_send_slot`)
- [x] retry wiring uses the adapter's inline cap; plain meta on fallback (`::test_send_with_retry_uses_the_adapters_cap`, `::test_plain_meta_reaches_the_adapter_on_fallback`)
- [x] emit to the sink, failures contained (`::test_emit_hands_events_to_the_sink_and_survives_failures`)
- [x] capability flags incl. `dm_is_private` (R06.24) (`::test_default_capabilities`)
- Deliberate differences: template methods instead of overridable public methods; typed connect error instead of a bool; private notices refused by default; media sends refused by default; our callback prefixes.
- Not taken (later tasks add what they need): `supports_code_blocks`, status text, `typed_command_prefix`, in-channel continuable cron (P3), `interactive_resume` (P15-26 may want it), shared-listener / profile-prefix flags (multiplexing), handoff threads, slash-confirm and pickers (P15-13), draft streaming and `REQUIRES_EDIT_FINALIZE` (P2), TTS hooks, plugin handler rewiring (P13 follow-up).

## Status

Done (2026-10-19). `src/indus/gateway/adapters/{__init__,base}.py`, `src/indus/gateway/core/send_slot.py`, `tests/unit/gateway/adapters/{__init__,test_base}.py`, `tests/unit/gateway/core/test_send_slot.py`. Inbound conventions (edited / button / mentioned metadata) are in `inbound-events.md`.
