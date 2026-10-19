"""P15-10: bot-loop guard (R06.10) and the concurrent-session cap (K19)."""

from __future__ import annotations

from typing import Any

import pytest

from indus.gateway.core.bot_loop import (
    CONCURRENCY_REFUSAL,
    BotLoopGuard,
    BotLoopSettings,
    ConcurrencyCap,
    cap_from_value,
    conversation_of,
    settings_reader,
)
from tests.fakes.clock import FakeClock
from tests.unit.gateway.builders import replace, src


@pytest.fixture
def mono(fake_clock: FakeClock) -> FakeClock:
    return fake_clock


def _guard(clock: FakeClock, settings: BotLoopSettings | None = None) -> BotLoopGuard:
    current = settings or BotLoopSettings()
    return BotLoopGuard(lambda: current, monotonic=clock.monotonic)


BOT_A = src("discord", "chan1", "group", user_id="botA", is_bot=True)
BOT_B = replace(BOT_A, user_id="botB")


def test_two_bots_share_one_budget(mono: FakeClock) -> None:
    guard = _guard(mono)
    conv_a, conv_b = conversation_of(BOT_A, "work"), conversation_of(BOT_B, "work")
    assert conv_a == conv_b == ("work", "discord", "chan1")
    states = []
    for n in range(21):
        states.append(guard.admit(conv_a if n % 2 else conv_b))
        mono.advance(1)
    assert states[:20] == ["ok"] * 20
    assert states[20] == "tripped"
    assert guard.blocked(conv_a) and guard.blocked(conv_b)
    assert guard.admit(conv_a) == "cooldown"


def test_other_chats_and_profiles_have_their_own_budget(mono: FakeClock) -> None:
    guard = _guard(mono, BotLoopSettings(max_messages=1))
    assert guard.admit(conversation_of(BOT_A, "work")) == "ok"
    assert guard.admit(conversation_of(BOT_A, "work")) == "tripped"
    assert guard.admit(conversation_of(replace(BOT_A, chat_id="chan2"), "work")) == "ok"
    assert guard.admit(conversation_of(BOT_A, "play")) == "ok"


def test_blocked_is_read_only(mono: FakeClock) -> None:
    guard = _guard(mono, BotLoopSettings(max_messages=2))
    conv = conversation_of(BOT_A)
    for _ in range(50):
        assert not guard.blocked(conv)
    assert guard.admit(conv) == "ok"
    for _ in range(50):
        guard.blocked(conv)
    assert guard.admit(conv) == "ok"
    assert guard.admit(conv) == "tripped"


def test_same_message_counted_once(mono: FakeClock) -> None:
    guard = _guard(mono, BotLoopSettings(max_messages=2))
    conv = conversation_of(BOT_A)
    for _ in range(5):
        assert guard.admit(conv, message_id="m1") == "ok"
    assert guard.admit(conv, message_id="m2") == "ok"
    assert guard.admit(conv, message_id="m3") == "tripped"


def test_cooldown_expires_and_window_cleared(mono: FakeClock) -> None:
    guard = _guard(mono, BotLoopSettings(max_messages=2, cooldown_seconds=600))
    conv = conversation_of(BOT_A)
    guard.admit(conv)
    guard.admit(conv)
    assert guard.admit(conv) == "tripped"
    mono.advance(599)
    assert guard.blocked(conv) and guard.admit(conv) == "cooldown"
    mono.advance(1)
    assert not guard.blocked(conv)
    # The window was cleared when it tripped: a full budget again.
    assert [guard.admit(conv) for _ in range(3)] == ["ok", "ok", "tripped"]


def test_window_slides(mono: FakeClock) -> None:
    guard = _guard(mono, BotLoopSettings(max_messages=2, window_seconds=300))
    conv = conversation_of(BOT_A)
    assert guard.admit(conv) == "ok"
    mono.advance(200)
    assert guard.admit(conv) == "ok"
    mono.advance(101)  # the first message left the window
    assert guard.admit(conv) == "ok"
    assert guard.admit(conv) == "tripped"


def test_disabled_and_reread_every_call(mono: FakeClock) -> None:
    live = {"enabled": False, "max_messages": 1}
    guard = BotLoopGuard(lambda: BotLoopSettings.from_mapping(live), monotonic=mono.monotonic)
    conv = conversation_of(BOT_A)
    assert [guard.admit(conv) for _ in range(3)] == ["disabled"] * 3
    live["enabled"] = True
    assert guard.admit(conv) == "ok"
    assert guard.admit(conv) == "tripped"
    assert guard.blocked(conv)
    live["enabled"] = False
    assert not guard.blocked(conv)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, BotLoopSettings()),
        ("nonsense", BotLoopSettings()),
        ({}, BotLoopSettings()),
        ({"enabled": "off"}, BotLoopSettings(enabled=False)),
        ({"enabled": "maybe"}, BotLoopSettings()),
        ({"max_messages": 0}, BotLoopSettings()),
        ({"max_messages": -3}, BotLoopSettings()),
        ({"max_messages": 2.5}, BotLoopSettings()),
        ({"max_messages": True}, BotLoopSettings()),
        ({"max_messages": "5"}, BotLoopSettings(max_messages=5)),
        ({"window_seconds": "nan"}, BotLoopSettings()),
        ({"window_seconds": "inf"}, BotLoopSettings()),
        (
            {"window_seconds": 60, "cooldown_seconds": "30"},
            BotLoopSettings(window_seconds=60, cooldown_seconds=30),
        ),
    ],
)
def test_settings_fall_back_to_defaults(raw: Any, expected: BotLoopSettings) -> None:
    assert BotLoopSettings.from_mapping(raw) == expected


def test_settings_reader_reads_config_each_time() -> None:
    data: dict[str, Any] = {"gateway.bot_loop_guard": {"max_messages": 3}}
    read = settings_reader(data.get)
    assert read().max_messages == 3
    data["gateway.bot_loop_guard"] = {"max_messages": 4}
    assert read().max_messages == 4

    def broken(key: str) -> object:
        raise RuntimeError("config gone")

    assert settings_reader(broken)() == BotLoopSettings()


def test_broken_settings_callable_uses_defaults(mono: FakeClock) -> None:
    def broken() -> BotLoopSettings:
        raise RuntimeError("x")

    guard = BotLoopGuard(broken, monotonic=mono.monotonic)
    assert guard.admit(conversation_of(BOT_A)) == "ok"


def test_idle_conversations_swept_once_per_window(mono: FakeClock) -> None:
    guard = _guard(mono, BotLoopSettings(window_seconds=300))
    for n in range(10):
        guard.admit(("p", "x", str(n)))
    assert guard.tracked == 10
    mono.advance(301)
    guard.admit(("p", "x", "fresh"))
    assert guard.tracked == 1
    for n in range(5):
        guard.admit(("p", "y", str(n)))
    mono.advance(10)  # inside the window since the last sweep: no sweep yet
    guard.admit(("p", "z", "1"))
    assert guard.tracked == 7


def test_concurrency_cap(mono: FakeClock) -> None:
    limit: list[object] = [2]
    cap = ConcurrencyCap(lambda: limit[0])  # type: ignore[arg-type,return-value]
    assert cap.try_claim("a") and cap.try_claim("b")
    assert not cap.try_claim("c")
    assert cap.try_claim("a")  # a running session is not counted twice
    assert cap.active == 2
    cap.release("a")
    assert cap.try_claim("c")
    limit[0] = None
    assert cap.try_claim("d") and cap.active == 3
    limit[0] = "junk"
    assert cap.try_claim("e")
    assert "try again" in CONCURRENCY_REFUSAL


def test_cap_values() -> None:
    assert cap_from_value(3) == 3
    assert cap_from_value("4") == 4
    for bad in (None, 0, -1, 1.5, True, "x"):
        assert cap_from_value(bad) is None

    def broken() -> int:
        raise RuntimeError("x")

    assert ConcurrencyCap(broken).try_claim("k")
