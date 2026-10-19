"""The shared adapter contract suite (P15-01), parametrised over every registered adapter.

Each check is a named rule; a violation fails with ``contract[<name>]: ...``. The suite runs
against a trivial echo adapter defined here plus every case in ``ADAPTER_CASES`` (P15-33..35,
P16). Three seeded misbehaving echo variants prove the checks bite: one opens a real socket, one
swallows a rate limit, one re-sends a split payload whole.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import pytest

from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    ApprovalAction,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    ClarifyPrompt,
    approval_callback,
    clarify_callback,
)
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import ErrorKind, SendResult, is_ambiguous_timeout
from tests.contract.gateway.conftest import (
    ADAPTER_CASES,
    AdapterCase,
    CaseEnv,
    RealNetworkUsed,
    SocketGuard,
    pump,
    run_check,
    settle,
)
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakePlatformError,
)


def expect(condition: object, rule: str, detail: str) -> None:
    if not condition:
        raise AssertionError(f"contract[{rule}]: {detail}")


# -- the trivial echo adapter -------------------------------------------------------------------


def split_text(text: str, limit: int, len_fn: Callable[[str], int]) -> list[str]:
    """Contiguous chunks under ``limit`` (cut after the last space when there is one)."""
    chunks: list[str] = []
    rest = text
    while rest:
        if len_fn(rest) <= limit:
            chunks.append(rest)
            break
        low, high = 1, len(rest)
        while low < high:
            mid = (low + high + 1) // 2
            if len_fn(rest[:mid]) <= limit:
                low = mid
            else:
                high = mid - 1
        cut = low
        space = rest.rfind(" ", 0, cut)
        if space > 0:
            cut = space + 1
        chunks.append(rest[:cut])
        rest = rest[cut:]
    return chunks


_KINDS = {
    "voice": MessageKind.VOICE,
    "audio": MessageKind.AUDIO,
    "photo": MessageKind.PHOTO,
    "video": MessageKind.VIDEO,
    "document": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
}


class EchoAdapter(Adapter):
    platform: ClassVar[str] = "echo"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=64,
        splits_natively=True,
        supports_edit=True,
        supports_delete=True,
        supports_buttons=True,
    )

    def __init__(self, fake: FakeChatPlatform, **kw: Any) -> None:
        super().__init__(**kw)
        self.fake = fake
        self._reader: asyncio.Task[None] | None = None

    async def _open(self, is_reconnect: bool) -> None:
        try:
            await self.fake.connect()
        except FakePlatformError as exc:
            raise AdapterConnectError(str(exc), retryable=True) from None
        self._reader = asyncio.ensure_future(self._read())

    async def _close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        await self.fake.disconnect()

    async def _read(self) -> None:
        while True:
            item = await self.fake.receive_inbound()
            await self.emit(self.normalise(item))

    def normalise(self, item: FakeInbound) -> InboundEvent:
        chat_type = "thread" if item.thread_id else item.chat_kind
        source = Source(
            platform=self.platform,
            chat_id=item.chat_id,
            chat_type=chat_type,
            chat_name=item.chat_name or None,
            user_id=item.user_id,
            user_name=item.user_name or None,
            thread_id=item.thread_id,
            is_bot=item.is_bot,
        )
        metadata: dict[str, Any] = {}
        if item.event == "edited":
            metadata["edited"] = True
        if item.event == "button":
            metadata["button"] = item.button_data
        if item.mentions_bot:
            metadata["mentioned"] = True
        media = tuple(Attachment(ref=m.ref, mime=m.mime) for m in item.media)
        kind = _KINDS.get(item.media[0].kind, MessageKind.TEXT) if item.media else MessageKind.TEXT
        return InboundEvent(
            source=source,
            text=item.text,
            kind=kind,
            message_id=item.message_id or None,
            update_id=item.update_id,
            media=media,
            reply_to_id=item.reply_to,
            metadata=metadata,
            timestamp=item.at,
        )

    def failure(self, exc: FakePlatformError, delivered: list[str], rest: list[str]) -> SendResult:
        remainder = None if is_ambiguous_timeout(exc) else "".join(rest)
        return SendResult.failure(
            exc=exc,
            retry_after=exc.retry_after,
            delivered_ids=tuple(delivered),
            remainder=remainder if delivered else None,
        )

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        caps = self.capabilities
        chunks = split_text(text, caps.max_len, caps.len_fn)
        ids: list[str] = []
        for n, chunk in enumerate(chunks):
            if n:
                await self.slots.pace(chat_id)
            try:
                sent = await self.fake.post(
                    chat_id,
                    chunk,
                    reply_to=reply_to if n == 0 else None,
                    thread_id=meta.get("thread_id"),
                )
            except FakePlatformError as exc:
                return self.failure(exc, ids, chunks[n:])
            ids.append(sent.message_id)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        try:
            await self.fake.edit(chat_id, message_id, text)
        except FakePlatformError as exc:
            return self.failure(exc, [], [])
        return SendResult.sent(message_id)

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        return await self.fake.delete(chat_id, message_id)

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        await self.fake.typing(chat_id)

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = tuple(
            Button(a.label, approval_callback(prompt.request_id, a.choice)) for a in prompt.actions
        )
        try:
            sent = await self.fake.post(chat_id, prompt.text, buttons=buttons)
        except FakePlatformError as exc:
            return self.failure(exc, [], [])
        return SendResult.sent(sent.message_id)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = tuple(
            Button(choice, clarify_callback(prompt.clarify_id, n))
            for n, choice in enumerate(prompt.choices)
        )
        sent = await self.fake.post(chat_id, prompt.question, buttons=buttons)
        return SendResult.sent(sent.message_id)

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        chat = await self.fake.chat(chat_id)
        return ChatInfo(chat.name, chat.kind)


def echo_case(cls: type[EchoAdapter] = EchoAdapter, name: str = "echo") -> AdapterCase:
    def make_env(clock: FakeClock) -> CaseEnv:
        return CaseEnv(clock, FakeChatPlatform("echo", clock=clock, max_len=64))

    def build(env: CaseEnv) -> Adapter:
        return cls(env.platform, clock=env.clock)

    return AdapterCase(name, make_env, build)


CASES = [*ADAPTER_CASES, echo_case()]

# -- the checks ---------------------------------------------------------------------------------

PROMPT = ApprovalPrompt(
    request_id="req1",
    session_key="key",
    text="Run this command? rm -rf build",
    fallback_text="Run this command? rm -rf build\nReply /approve or /deny.",
    actions=(
        ApprovalAction("Allow once", "once", "primary"),
        ApprovalAction("Allow for this session", "session"),
        ApprovalAction("Always allow", "always"),
        ApprovalAction("Deny", "deny", "danger"),
    ),
)

_INDICATOR = re.compile(r"^\(\d+/\d+\)$")


def _long_text(caps: Capabilities) -> str:
    words: list[str] = []
    n = 0
    while caps.len_fn(" ".join(words)) < caps.max_len * 3.5:
        n += 1
        words.append(f"w{n:05d}" if n % 7 else f"w{n:05d}😀")
    return " ".join(words)


def _words(texts: list[str]) -> list[str]:
    return [w for t in texts for w in t.split() if not _INDICATOR.match(w)]


async def check_lifecycle(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    await settle(env, adapter.connect())
    await settle(env, adapter.connect())
    expect(adapter.connected, "lifecycle", "connect() did not leave the adapter connected")
    connects = env.platform.ops_of("connect")
    expect(len(connects) == 1, "lifecycle", f"a second connect() reconnected ({len(connects)})")
    await settle(env, adapter.disconnect())
    await settle(env, adapter.disconnect())
    expect(not adapter.connected, "lifecycle", "disconnect() left the adapter connected")
    disconnects = env.platform.ops_of("disconnect")
    expect(len(disconnects) == 1, "lifecycle", f"a second disconnect() ran ({len(disconnects)})")
    await settle(env, adapter.connect(is_reconnect=True))
    expect(adapter.connected, "lifecycle", "reconnect after disconnect failed")


async def check_connect_failure(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    env.platform.fail_connect(FailureKind.CONNECT_TIMEOUT)
    raised: AdapterConnectError | None = None
    try:
        await settle(env, adapter.connect())
    except AdapterConnectError as exc:
        raised = exc
    expect(raised is not None, "connect-failure", "a failed connect must raise AdapterConnectError")
    expect(raised is not None and raised.retryable, "connect-failure", "a timeout is retryable")
    expect(not adapter.connected, "connect-failure", "a failed connect left it 'connected'")


async def check_send_ids(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    await settle(env, adapter.connect())
    result = await settle(env, adapter.send(case.group_id, "hello contract"))
    expect(result.ok and result.message_id, "send-ids", f"send failed or has no id: {result}")
    for message_id in result.ids:
        message = env.platform.message(message_id)
        expect(message is not None, "send-ids", f"id {message_id} is not on the platform")
    texts = " ".join(env.platform.texts(case.group_id))
    expect("hello contract" in texts, "send-ids", f"text not delivered: {texts!r}")


async def check_split(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    caps = adapter.capabilities
    await settle(env, adapter.connect())
    text = _long_text(caps)
    result = await settle(env, adapter.send(case.group_id, text))
    texts = env.platform.texts(case.group_id)
    if not caps.splits_natively:
        expect(not result.ok and result.kind is ErrorKind.TOO_LONG, "split", "expected too_long")
        expect(texts == [], "split", "a refused long text reached the platform")
        return
    expect(result.ok, "split", f"long send failed: {result.error}")
    expect(len(result.ids) >= 3, "split", f"3.5x the cap in {len(result.ids)} message(s)")
    for message in env.platform.messages(case.group_id):
        size = caps.len_fn(message.text)
        expect(size <= caps.max_len, "split", f"chunk of {size} over the cap {caps.max_len}")
    expect(
        set(result.ids) == {m.message_id for m in env.platform.messages(case.group_id)},
        "split",
        "result ids differ from the delivered messages",
    )
    expect(_words(texts) == text.split(), "split", "words lost, reordered or repeated")


async def check_no_whole_resend(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    caps = adapter.capabilities
    if not caps.splits_natively:
        return
    await settle(env, adapter.connect())
    env.platform.fail(FailureKind.CONNECT_TIMEOUT, op="send", after=1)
    text = _long_text(caps)
    await settle(env, adapter.send_with_retry(case.group_id, text, rng=lambda: 0.0))
    counts = Counter(_words(env.platform.texts(case.group_id)))
    repeated = sorted(w for w, c in counts.items() if c > 1)
    expect(not repeated, "no-whole-resend", f"a split payload was re-sent: {repeated[:3]}")


async def check_edit(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    await settle(env, adapter.connect())
    sent = await settle(env, adapter.send(case.group_id, "draft"))
    expect(sent.ok and sent.message_id, "edit", "could not send the message to edit")
    assert sent.message_id is not None
    result = await settle(env, adapter.edit(case.group_id, sent.message_id, "final", final=True))
    if adapter.capabilities.supports_edit:
        expect(result.ok, "edit", f"final edit failed: {result.error}")
        message = env.platform.message(sent.message_id)
        expect(message is not None and "final" in message.text, "edit", "edit not applied")
    else:
        expect(result.unsupported, "edit", "an adapter without edits must refuse cleanly")
        expect(not env.platform.ops_of("edit"), "edit", "a refused edit reached the platform")


async def check_typing(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    await settle(env, adapter.connect())
    await settle(env, adapter.typing_start(case.group_id))
    await settle(env, adapter.typing_start(case.group_id))
    count = len(env.platform.ops_of("typing"))
    expect(count in (0, 2), "typing-refresh", f"two typing refreshes gave {count} indicator(s)")
    await settle(env, adapter.typing_stop(case.group_id))


async def check_approvals(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    await settle(env, adapter.connect())
    result = await settle(env, adapter.send_approval(case.user_id, PROMPT))
    expect(result.ok and result.message_id, "approvals", f"approval card failed: {result.error}")
    assert result.message_id is not None
    message = env.platform.message(result.message_id)
    expect(message is not None, "approvals", "approval card is not on the platform")
    assert message is not None
    if adapter.capabilities.supports_buttons:
        wanted = [approval_callback(PROMPT.request_id, a.choice) for a in PROMPT.actions]
        datas = [b.data for b in message.buttons]
        expect(datas == wanted, "approvals", f"buttons {datas} instead of {wanted}")
    else:
        expect("/approve" in message.text, "approvals", "text fallback lacks the /approve hint")


async def check_inbound(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    events: list[InboundEvent] = []

    async def sink(event: InboundEvent) -> None:
        events.append(event)

    adapter.set_sink(sink)
    await settle(env, adapter.connect())
    p = env.platform
    dm = p.dm(case.user_id, "hello there", user_name="Ana")
    p.group(case.group_id, case.user_id, "/help", mention=True)
    p.thread(case.group_id, case.thread_id, case.user_id, "in a thread")
    p.bot_message(case.group_id, case.bot_id, "beep")
    p.voice(case.user_id, case.user_id)
    p.photo(case.user_id, case.user_id, caption="look")
    p.document(case.user_id, case.user_id)
    p.edited(dm, "hello again")
    if adapter.capabilities.supports_buttons:
        p.button_press(case.user_id, case.user_id, approval_callback("req1", "once"))
    await pump(case, env, adapter)

    def find(rule: str, test: Callable[[InboundEvent], bool]) -> InboundEvent:
        found = [e for e in events if test(e)]
        expect(found, "inbound", f"no event for {rule} among {len(events)}")
        return found[0]

    for event in events:
        expect(event.source.platform == adapter.platform, "inbound", "wrong source.platform")
    first = find("the DM", lambda e: e.text == "hello there")
    expect(first.source.chat_type == "dm", "inbound", f"DM typed {first.source.chat_type}")
    expect(first.user_id == case.user_id, "inbound", "DM sender id lost")
    expect(first.message_id, "inbound", "DM without a message id")
    expect(not first.source.is_bot, "inbound", "a person marked as a bot")
    command = find("the group command", lambda e: e.command == "help")
    expect(command.source.chat_type in ("group", "channel"), "inbound", "group chat type")
    expect(command.source.chat_id == case.group_id, "inbound", "group chat id lost")
    thread = find("the thread message", lambda e: e.text == "in a thread")
    expect(
        thread.source.chat_type in ("thread", "forum")
        and case.thread_id in (thread.source.thread_id, thread.source.chat_id),
        "inbound",
        f"thread not normalised: {thread.source}",
    )
    bot = find("the bot message", lambda e: e.text == "beep")
    expect(bot.source.is_bot, "inbound", "a bot-authored message not marked is_bot")
    voice = find("the voice note", lambda e: e.kind is MessageKind.VOICE)
    expect(len(voice.media) == 1, "inbound", "voice note without its attachment")
    find("the photo", lambda e: e.kind is MessageKind.PHOTO and "look" in e.text)
    find("the document", lambda e: e.kind is MessageKind.DOCUMENT)
    for edit in [e for e in events if e.text == "hello again"]:
        expect(edit.metadata.get("edited") is True, "inbound", "an edit not marked as edited")
    if adapter.capabilities.supports_buttons:
        data = approval_callback("req1", "once")
        find("the button press", lambda e: e.metadata.get("button") == data)


EXPECTED: dict[FailureKind, Callable[[SendResult], bool]] = {
    FailureKind.CONNECT_TIMEOUT: lambda r: r.kind is ErrorKind.TRANSIENT and r.retryable,
    FailureKind.READ_TIMEOUT: lambda r: r.maybe_delivered and not r.retryable,
    FailureKind.RATE_LIMIT: lambda r: r.kind is ErrorKind.RATE_LIMITED and r.retry_after == 7,
    FailureKind.CHAT_NOT_FOUND: lambda r: r.kind is ErrorKind.NOT_FOUND and r.chat_unreachable,
    FailureKind.THREAD_NOT_FOUND: lambda r: (
        r.kind is ErrorKind.NOT_FOUND and not r.chat_unreachable
    ),
    FailureKind.FORBIDDEN: lambda r: r.kind is ErrorKind.FORBIDDEN and r.chat_unreachable,
    FailureKind.PARSE_ERROR: lambda r: r.kind is ErrorKind.BAD_FORMAT,
    FailureKind.TOO_LONG: lambda r: r.kind is ErrorKind.TOO_LONG,
}


async def check_errors(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    await settle(env, adapter.connect())
    for kind, ok in EXPECTED.items():
        env.platform.fail(kind, op="send", chat_id=case.group_id, retry_after=7)
        result = await settle(env, adapter.send(case.group_id, "x"))
        expect(not result.ok, "error-classification", f"{kind}: injected failure reported ok")
        expect(ok(result), "error-classification", f"{kind}: classified as {result}")


async def scenario(case: AdapterCase, env: CaseEnv, adapter: Adapter) -> None:
    for check in (check_send_ids, check_edit, check_typing, check_approvals, check_errors):
        await check(case, env, adapter)


CHECKS: dict[str, Callable[[AdapterCase, CaseEnv, Adapter], Awaitable[None]]] = {
    "lifecycle": check_lifecycle,
    "connect-failure": check_connect_failure,
    "send-ids": check_send_ids,
    "split": check_split,
    "no-whole-resend": check_no_whole_resend,
    "edit": check_edit,
    "typing-refresh": check_typing,
    "approvals": check_approvals,
    "inbound": check_inbound,
    "error-classification": check_errors,
}


def check_no_real_network(case: AdapterCase, guard: SocketGuard) -> None:
    run_check(case, scenario)
    expect(not guard.attempts, "no-real-network", f"real connects: {guard.attempts}")


def _ids(case: AdapterCase) -> str:
    return case.name


@pytest.mark.parametrize("case", CASES, ids=_ids)
@pytest.mark.parametrize("rule", sorted(CHECKS))
def test_adapter_contract(case: AdapterCase, rule: str) -> None:
    run_check(case, CHECKS[rule])


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_adapter_uses_no_real_network(case: AdapterCase, socket_guard: SocketGuard) -> None:
    check_no_real_network(case, socket_guard)


# -- the seeded misbehaving adapters --------------------------------------------------------------


class NetworkyEcho(EchoAdapter):
    """Opens a real connection on every send and hides the error."""

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        with contextlib.suppress(Exception):
            socket.create_connection(("192.0.2.1", 443), timeout=0.01)
        return await super()._send(chat_id, text, reply_to, meta)


class FloodSwallowingEcho(EchoAdapter):
    """Turns a rate limit into a vague failure, losing the server's wait."""

    def failure(self, exc: FakePlatformError, delivered: list[str], rest: list[str]) -> SendResult:
        if exc.kind is FailureKind.RATE_LIMIT:
            return SendResult.failure("temporary problem, try later")
        return super().failure(exc, delivered, rest)


class WholeResendEcho(EchoAdapter):
    """Retries a partly delivered split payload by sending all of it again."""

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        result = await super()._send(chat_id, text, reply_to, meta)
        if result.partial:
            return await super()._send(chat_id, text, reply_to, meta)
        return result


@pytest.mark.parametrize(
    ("cls", "rule"),
    [
        (NetworkyEcho, "no-real-network"),
        (FloodSwallowingEcho, "error-classification"),
        (WholeResendEcho, "no-whole-resend"),
    ],
)
def test_misbehaving_adapters_fail_the_named_check(
    cls: type[EchoAdapter], rule: str, socket_guard: SocketGuard
) -> None:
    case = echo_case(cls, name=cls.__name__)
    with pytest.raises(AssertionError, match=rf"^contract\[{re.escape(rule)}\]"):
        if rule == "no-real-network":
            check_no_real_network(case, socket_guard)
        else:
            run_check(case, CHECKS[rule])


def test_misbehaving_adapters_still_pass_the_other_checks() -> None:
    """Each seeded fault is caught by its own rule, not by an unrelated one."""
    for cls, rule in (
        (FloodSwallowingEcho, "error-classification"),
        (WholeResendEcho, "no-whole-resend"),
    ):
        case = echo_case(cls, name=cls.__name__)
        for other, check in CHECKS.items():
            if other != rule:
                run_check(case, check)


def test_socket_guard_raises_on_any_connect(socket_guard: SocketGuard) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RealNetworkUsed):
            sock.connect(("127.0.0.1", 9))
        with pytest.raises(RealNetworkUsed):
            sock.connect_ex(("127.0.0.1", 9))
    finally:
        sock.close()
    with pytest.raises(RealNetworkUsed):
        socket.create_connection(("example.invalid", 443))

    async def via_asyncio() -> None:
        await asyncio.open_connection("127.0.0.1", 9)

    with pytest.raises(RealNetworkUsed):
        asyncio.run(via_asyncio())
    assert len(socket_guard.attempts) == 4


def test_every_case_has_a_unique_name() -> None:
    names = [case.name for case in CASES]
    assert len(names) == len(set(names))
    assert "echo" in names
