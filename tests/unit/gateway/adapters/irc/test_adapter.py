"""P16-27: IRC specifics on the fake server (the shared contract runs in tests/contract)."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.irc import CHANNEL_ENV, NICK_ENV, SERVER_ENV, create_adapter
from indus.gateway.adapters.irc.adapter import CONFIRM_S, IrcAdapter, IrcOptions
from indus.gateway.adapters.irc.protocol import (
    IrcLine,
    format_line,
    payload_budget,
    sasl_plain_chunks,
    split_payload,
    utf8_bytes,
)
from indus.gateway.config import AdapterConfig, load_gateway_settings, lock_key
from indus.gateway.core.events import InboundEvent
from indus.gateway.core.send import ErrorKind
from indus.gateway.registry import PlatformRegistry
from indus.gateway.token_locks import TokenLocks
from tests.contract.gateway import irc_case
from tests.contract.gateway.conftest import settle
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.irc_wire import IrcWire

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
NICK = irc_case.NICK
# Built at runtime: never a credential-shaped literal in the source.
PASSWORD = "".join(["irc", "-pw-", "9a8b" * 3])


def run[T](coro: Awaitable[T]) -> T:
    async def main() -> T:
        return await coro

    return asyncio.run(main())


class Rig:
    def __init__(
        self, options: IrcOptions | None = None, *, wire_kw: dict[str, Any] | None = None, **kw: Any
    ) -> None:
        self.clock = FakeClock(START)
        self.env = irc_case.make_env(self.clock)
        if wire_kw:
            self.env.wire = IrcWire(self.env.platform, **wire_kw)
        self.wire: IrcWire = self.env.wire
        self.platform = self.env.platform
        self.adapter = irc_case.build_adapter(self.env, options, **kw)
        self.events: list[InboundEvent] = []
        self.fatal: list[tuple[str, str]] = []
        self.adapter.set_sink(self._sink)
        self.adapter.set_fatal_listener(lambda code, message: self.fatal.append((code, message)))

    async def _sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def settle[T](self, awaitable: Awaitable[T]) -> T:
        return await settle(self.env, awaitable)

    async def connect(self) -> None:
        await self.settle(self.adapter.connect())
        await self.clock.run_until_idle()

    async def wait(self, seconds: float) -> None:
        end = self.clock.monotonic() + seconds
        await self.clock.run_until_idle()
        while self.clock.monotonic() < end:
            await self.clock.advance_and_settle(min(0.5, end - self.clock.monotonic()))

    def server_says(self, line: str) -> None:
        self.wire.current.send(line)


def _sent(rig: Rig) -> list[IrcLine]:
    return [line for raw in rig.wire.received() if (line := IrcLine.parse(raw)) is not None]


def options(**kw: Any) -> IrcOptions:
    base: dict[str, Any] = {"server": "irc.fake.test", "nickname": NICK, "channels": ("#indus",)}
    return IrcOptions(**{**base, **kw})


# -- splitting ----------------------------------------------------------------------------------


def test_multibyte_text_splits_at_byte_boundaries() -> None:
    text = "héllo wörld " * 30 + "😀" * 200 + " ünïcode" * 40
    pieces = split_payload(text, 450)
    assert len(pieces) > 3
    for piece, offset in pieces:
        assert utf8_bytes(piece) <= 450
        piece.encode("utf-8")  # whole characters only
        assert text[offset:].startswith(piece)
    assert "".join(p for p, _ in pieces).replace(" ", "") == text.replace(" ", "")
    emoji_only = split_payload("😀" * 200, 450)
    assert [utf8_bytes(p) for p, _ in emoji_only] == [448, 352]
    assert payload_budget("#c") == 450
    # the whole line, CRLF included, stays within 512 bytes
    assert payload_budget("#" + "x" * 300) == 512 - utf8_bytes("PRIVMSG #" + "x" * 300 + " :") - 2


def test_multibyte_lines_reach_the_server_whole() -> None:
    rig = Rig()
    text = "über " * 150 + "\n\n" + "日本語のテキスト" * 40

    async def main() -> Any:
        await rig.connect()
        result = await rig.settle(rig.adapter.send("#indus", text))
        await rig.adapter.disconnect()
        return result

    result = run(main())
    sent = rig.platform.texts("#indus")
    assert result.ok and len(result.ids) == len(sent) > 2
    assert all(utf8_bytes(t) <= 450 for t in sent)
    assert "".join(sent).replace(" ", "") == text.replace(" ", "").replace("\n", "")


def test_line_breaks_and_control_characters_never_start_a_command() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        await rig.settle(rig.adapter.send("#indus", "hi\r\nQUIT :bye\x00\rJOIN #evil"))
        await rig.adapter.disconnect()

    run(main())
    commands = [IrcLine.parse(raw) for raw in rig.wire.received()]
    privmsgs = [c.param(-1) for c in commands if c is not None and c.command == "PRIVMSG"]
    assert privmsgs == ["hi", "QUIT :bye", "JOIN #evil"]
    assert "#evil" not in rig.wire.joined
    with pytest.raises(ValueError):
        format_line("PRIVMSG", "#a", "x\r\nQUIT")
    with pytest.raises(ValueError):
        format_line("PRIVMSG", "#a b", "x")
    for bad in ("#a,#b", "a b", ":x", ""):
        result = run(Rig().adapter.send(bad, "x"))
        assert not result.ok and result.kind is ErrorKind.NOT_FOUND


# -- registration -------------------------------------------------------------------------------


def test_nick_collision_recovers_with_underscores() -> None:
    rig = Rig()
    rig.wire.taken = {NICK, NICK + "_"}

    async def main() -> str:
        await rig.connect()
        nick = rig.adapter.nick
        await rig.adapter.disconnect()
        return nick

    assert run(main()) == NICK + "__"

    stuck = Rig()
    stuck.wire.taken = {NICK + "_" * n for n in range(4)}

    async def refused() -> AdapterConnectError:
        with pytest.raises(AdapterConnectError) as caught:
            await stuck.connect()
        return caught.value

    error = run(refused())
    assert error.retryable and "taken" in str(error)
    nicks = [line for line in stuck.wire.received() if line.startswith("NICK")]
    assert nicks == [f"NICK {NICK}", f"NICK {NICK}_", f"NICK {NICK}__", f"NICK {NICK}___"]


def test_sasl_plain_over_tls_and_the_password_stays_out_of_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(
        options(sasl_username="indus", sasl_password=PASSWORD),
        wire_kw={"accounts": {"indus": PASSWORD}},
    )

    async def main() -> None:
        await rig.connect()
        await rig.settle(rig.adapter.send("#indus", "logged in"))
        await rig.adapter.disconnect()

    with caplog.at_level(logging.DEBUG):
        run(main())
    assert rig.wire.opens == [("irc.fake.test", 6697, True)]
    connection = rig.wire.history[0]
    assert connection.account == "indus" and connection.tls
    sent = connection.received
    assert sent[0] == "CAP LS 302"
    assert "CAP REQ :message-tags server-time echo-message sasl" in sent
    assert sent.index("AUTHENTICATE PLAIN") < sent.index("CAP END")
    assert PASSWORD not in caplog.text and PASSWORD not in repr(rig.adapter.options)
    assert PASSWORD not in rig.adapter.scrub(f"boom {PASSWORD}")
    wrong = Rig(options(sasl_password="nope"), wire_kw={"accounts": {"indus": PASSWORD}})

    async def refused() -> AdapterConnectError:
        with pytest.raises(AdapterConnectError) as caught:
            await wrong.connect()
        return caught.value

    error = run(refused())
    assert not error.retryable and error.code == "irc_sasl"
    long = sasl_plain_chunks("u", "p" * 300)
    assert len(long[0]) == 400 and base64.b64decode("".join(long)).endswith(b"p" * 300)


def test_a_password_is_never_sent_over_a_plain_connection() -> None:
    rig = Rig(options(use_tls=False, port=6667, nickserv_password=PASSWORD))

    async def refused() -> AdapterConnectError:
        with pytest.raises(AdapterConnectError) as caught:
            await rig.connect()
        return caught.value

    error = run(refused())
    assert not error.retryable and error.code == "irc_plaintext_auth"
    assert rig.wire.opens == [] and PASSWORD not in str(error)

    opted = Rig(
        options(use_tls=False, port=6667, nickserv_password=PASSWORD, allow_plaintext_auth=True)
    )

    async def main() -> None:
        await opted.connect()
        await opted.adapter.disconnect()

    run(main())
    assert opted.wire.opens == [("irc.fake.test", 6667, False)]
    assert f"PRIVMSG NickServ :IDENTIFY {PASSWORD}" in opted.wire.received()


# -- confirmation and pacing --------------------------------------------------------------------


def test_one_line_per_half_second_per_target() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        await rig.settle(rig.adapter.send("#indus", "warm up"))
        await rig.wait(5)
        start = rig.clock.monotonic()
        first = asyncio.ensure_future(rig.adapter.send("#indus", "one\ntwo\nthree"))
        second = asyncio.ensure_future(rig.adapter.send("#other", "elsewhere"))
        await rig.settle(asyncio.gather(first, second))
        rig.start = start  # type: ignore[attr-defined]
        await rig.adapter.disconnect()

    run(main())
    start: float = rig.start  # type: ignore[attr-defined]
    sends = [op for op in rig.platform.ops_of("send") if op.at >= start]
    times = {op.text: op.at - start for op in sends}
    assert times["one"] == 0.0 and times["two"] == 0.5 and times["three"] == 1.0
    assert times["elsewhere"] < 0.5  # another target is not held behind this one


def test_without_echo_a_ping_confirms_and_errors_are_still_read() -> None:
    caps = ("message-tags", "server-time")
    rig = Rig(wire_kw={"caps": caps})

    async def main() -> tuple[Any, Any]:
        await rig.connect()
        ok = await rig.settle(rig.adapter.send("#indus", "hello"))
        rig.platform.fail(FailureKind.CHAT_NOT_FOUND, op="send")
        gone = await rig.settle(rig.adapter.send("nobody", "hello?"))
        await rig.adapter.disconnect()
        return ok, gone

    ok, gone = run(main())
    assert ok.ok and ok.message_id is not None and ok.message_id.startswith("irc-")
    assert any(c.command == "PING" and c.param(0).startswith("indus-") for c in _sent(rig))
    assert not gone.ok and gone.kind is ErrorKind.NOT_FOUND and gone.chat_unreachable


def test_a_silent_server_means_maybe_delivered_and_a_partial_send_resumes() -> None:
    rig = Rig()

    async def main() -> tuple[Any, float, Any]:
        await rig.connect()
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="send")
        before = rig.clock.monotonic()
        silent = await rig.settle(rig.adapter.send("#indus", "x"))
        waited = rig.clock.monotonic() - before
        rig.platform.fail(FailureKind.FORBIDDEN, op="send", after=1)
        partial = await rig.settle(rig.adapter.send("#indus", "first line\nsecond line\nthird"))
        await rig.adapter.disconnect()
        return silent, waited, partial

    silent, waited, partial = run(main())
    assert silent.maybe_delivered and not silent.retryable and waited >= CONFIRM_S
    assert partial.partial and partial.kind is ErrorKind.FORBIDDEN
    assert len(partial.ids) == 1 and partial.remainder == "second line\nthird"


# -- inbound ------------------------------------------------------------------------------------


def test_inbound_lines_become_events() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        say = rig.server_says
        say("@time=2026-01-01T08:00:00.000Z :ana!a@h PRIVMSG #indus :IndusBot, status please")
        say(":ana!a@h PRIVMSG #indus :just chatting")
        say(":ana!a@h PRIVMSG IndusBot :\x01ACTION waves\x01")
        say(":ana!a@h PRIVMSG IndusBot :\x01VERSION\x01")
        say(":NickServ!s@services NOTICE IndusBot :You are now identified")
        say(":IndusBot!u@h PRIVMSG #indus :our own echo")
        say(":ana!a@h PRIVMSG IndusBot :same")
        say(":ana!a@h PRIVMSG IndusBot :same")
        say("@bot :helper!h@h PRIVMSG #indus :beep")
        say(":Ana!a@h PRIVMSG #indus :indusbot what time is it")
        say("PING :server-check")
        await rig.wait(1)
        await rig.adapter.disconnect()

    run(main())
    texts = [e.text for e in rig.events]
    assert texts == [
        "status please",
        "just chatting",
        "* ana waves",
        "same",
        "same",
        "beep",
        "what time is it",
    ]
    addressed, chatting, action, same1, same2, beep, spaced = rig.events
    assert spaced.metadata == {"mentioned": True}
    assert spaced.source.user_id == "Ana" and spaced.source.user_id_alt == "ana"
    assert addressed.source.user_id_alt is None
    assert addressed.metadata == {"mentioned": True} and addressed.source.chat_id == "#indus"
    assert addressed.timestamp == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    assert chatting.metadata == {} and chatting.source.chat_type == "group"
    assert action.source.chat_type == "dm" and action.source.chat_id == "ana"
    assert same1.message_id != same2.message_id
    assert beep.source.is_bot and not addressed.source.is_bot
    assert any(c.command == "PONG" and c.param(-1) == "server-check" for c in _sent(rig))


# -- connection loss ----------------------------------------------------------------------------


def test_a_dropped_connection_reconnects_and_a_ban_is_fatal() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        rig.wire.drop()
        await rig.wait(1)
        assert len(rig.wire.opens) == 1
        await rig.wait(1.5)
        assert len(rig.wire.opens) == 2 and rig.adapter.reopened == 1
        assert rig.wire.joined.count("#indus") == 2  # channels joined again
        assert (await rig.settle(rig.adapter.send("#indus", "back"))).ok
        rig.wire.ban()
        await rig.wait(120)
        await rig.adapter.disconnect()

    run(main())
    assert [code for code, _ in rig.fatal] == ["irc_banned"]
    assert len(rig.wire.opens) == 2


def test_a_silent_link_is_pinged_then_replaced() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.connect()
        rig.wire.mute()
        await rig.wait(240)
        assert any(c.command == "PING" and c.param(0) == "indus-alive" for c in _sent(rig))
        assert len(rig.wire.opens) == 1
        await rig.wait(243)
        await rig.adapter.disconnect()

    run(main())
    assert len(rig.wire.opens) == 2


def test_channels_are_joined_on_demand_and_a_closed_one_is_refused() -> None:
    rig = Rig()
    rig.wire.closed_channels = {"#secret"}

    async def main() -> tuple[Any, Any]:
        await rig.connect()
        joined = await rig.settle(rig.adapter.send("#new", "hello new"))
        refused = await rig.settle(rig.adapter.send("#secret", "let me in"))
        await rig.adapter.disconnect()
        return joined, refused

    joined, refused = run(main())
    assert joined.ok and rig.wire.joined == ["#indus", "#new"]
    assert not refused.ok and refused.kind is ErrorKind.FORBIDDEN
    assert rig.platform.texts("#secret") == []


# -- wiring -------------------------------------------------------------------------------------


def _config(scope: Scope, *, send_only: bool = False, extra: str = "") -> AdapterConfig:
    secrets: Any = scope.secrets
    secrets.set(SERVER_ENV, "irc.fake.test")
    secrets.set(NICK_ENV, NICK)
    secrets.set(CHANNEL_ENV, "#indus,#ops")
    secrets.set("IRC_NICKSERV_PASSWORD", PASSWORD)
    if extra:
        from tests.unit.cli.commands.cmdrun import write_settings

        write_settings(scope, extra)
    settings = load_gateway_settings(scope).get("irc")
    assert settings is not None and settings.enabled
    return AdapterConfig("irc", settings, scope, scope.clock, None, send_only=send_only)


def test_create_adapter_reads_the_settings(scope: Scope) -> None:
    adapter = create_adapter(_config(scope))
    assert isinstance(adapter, IrcAdapter)
    opts = adapter.options
    assert (opts.server, opts.port, opts.use_tls, opts.channels) == (
        "irc.fake.test",
        6697,
        True,
        ("#indus", "#ops"),
    )
    assert PASSWORD not in repr(opts) and PASSWORD not in adapter.scrub(PASSWORD)
    plain = create_adapter(_config(scope, extra="platforms:\n  irc:\n    use_tls: false\n"))
    assert plain.options.port == 6667 and not plain.options.use_tls
    secrets: Any = scope.secrets
    secrets.set("IRC_PORT", "7000")
    assert create_adapter(_config(scope)).options.port == 7000
    with pytest.raises(ValueError, match="channel"):
        create_adapter(_config(scope, extra="platforms:\n  irc:\n    channels: [nochannel]\n"))


def test_the_token_lock_key_is_server_and_nick(scope: Scope, tmp_path: Path) -> None:
    entry = PlatformRegistry().get("irc")
    assert entry is not None and entry.lock_env == (SERVER_ENV, NICK_ENV)
    config = _config(scope)
    credential = lock_key(scope, config.settings, entry.lock_env)  # what the daemon locks on
    assert credential == f"irc.fake.test\n{NICK}"  # no channel, no password
    # The server and nick are not secrets: nothing masks them in logs.
    adapter = create_adapter(config)
    assert adapter.scrub(f"{NICK} on irc.fake.test") == f"{NICK} on irc.fake.test"

    def locks(pid: int) -> TokenLocks:
        return TokenLocks(
            tmp_path, profile=f"p{pid}", pid=pid, start_time=lambda _p: 1, alive=lambda _p: True
        )

    assert locks(1001).acquire("irc", credential, startup=True).acquired
    clash = locks(1002).acquire("irc", credential, startup=True)
    assert not clash.acquired and clash.fatal
    other_nick = f"irc.fake.test\n{NICK}2"
    assert locks(1002).acquire("irc", other_nick, startup=True).acquired


def test_indus_send_uses_its_own_nick_and_joins_only_the_target(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(START)
    env = irc_case.make_env(clock)
    wire: IrcWire = env.wire
    monkeypatch.setattr("indus.gateway.adapters.irc.CONNECTOR", wire.connect)
    adapter = create_adapter(_config(scope, send_only=True))

    async def main() -> Any:
        await adapter.connect()
        result = await settle(env, adapter.send("#ops", "deploy done"))
        await adapter.disconnect()
        return result

    assert run(main()).ok
    assert adapter.nick == f"{NICK}-send"
    assert wire.joined == ["#ops"] and env.platform.texts("#ops") == ["deploy done"]
    sent = wire.received()
    assert sent[-1].startswith("QUIT") and not any("IDENTIFY" in line for line in sent)
