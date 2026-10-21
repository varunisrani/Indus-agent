"""P24-20: the `wake.*` handlers — consent stored only by an explicit on, owner per connection."""

from __future__ import annotations

import asyncio
import base64
import struct
from dataclasses import dataclass
from pathlib import Path

import pytest

from indus.foundation import config
from indus.foundation.scope import Scope
from indus.protocol.base import ProtocolModel
from indus.protocol.methods import ProtocolError
from indus.service import wake_rpc
from indus.service.wake_rpc import (
    WakeDetected,
    WakeFeedParams,
    WakeNoParams,
    WakeRpc,
    WakeStartParams,
    WakeStopParams,
)
from indus.tools.builtin.media import wake_word
from indus.tools.builtin.media.wake_word import FRAME_LENGTH, REARM_FRAMES, WakeService


@dataclass(eq=False)
class Ctx:
    scope: Scope


class FakeEngine:
    name = "fake"
    frame_length = FRAME_LENGTH

    def score(self, data: bytes) -> float:
        return struct.unpack("<h", data[:2])[0] / 1000


class NoMicrophone:
    def __init__(self) -> None:
        self.opened = 0

    def open(self, sample_rate: int, frame_length: int) -> object:
        self.opened += 1
        raise AssertionError("the microphone must not open in these tests")


@pytest.fixture
def model(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = scope.home / "hey_indus.onnx"
    path.write_bytes(b"model")
    monkeypatch.setattr(wake_word, "_module_present", lambda name: True)
    config.save(scope, {"wake_word": {"model": str(path)}})
    return path


def make(scope: Scope) -> tuple[WakeRpc, list[tuple[object, str, ProtocolModel]], NoMicrophone]:
    events: list[tuple[object, str, ProtocolModel]] = []
    mic = NoMicrophone()
    rpc = WakeRpc(scope, emit=lambda owner, name, payload: events.append((owner, name, payload)))
    rpc.service = WakeService(
        lambda: wake_rpc.settings_for(scope),
        on_detect=rpc._detected,  # pyright: ignore[reportPrivateUsage]
        microphone=mic,  # type: ignore[arg-type]
        engine_factory=lambda s: FakeEngine(),
    )
    return rpc, events, mic


def test_rows_and_the_start_deadline() -> None:
    assert [row[0] for row in wake_rpc.WAKE_METHODS] == [
        "wake.start",
        "wake.stop",
        "wake.pause",
        "wake.resume",
        "wake.status",
        "wake.feed",
    ]
    assert wake_rpc.WAKE_DEADLINES == {"wake.start": 180.0}


def test_auto_arm_without_consent_refuses_and_writes_nothing(scope: Scope, model: Path) -> None:
    rpc, _events, mic = make(scope)
    ctx = Ctx(scope)
    answer = asyncio.run(rpc.start(ctx, WakeStartParams(surface="gui", client_capture=True)))
    assert not answer.started and answer.reason == "disabled"
    assert config.load(scope).get("wake_word.consent") is False
    assert mic.opened == 0


def test_explicit_on_stores_consent_then_detection_is_emitted(scope: Scope, model: Path) -> None:
    rpc, events, mic = make(scope)
    ctx = Ctx(scope)
    answer = asyncio.run(
        rpc.start(ctx, WakeStartParams(surface="gui", consent=True, client_capture=True))
    )
    assert answer.started and answer.consent_saved and answer.capture == "client"
    assert config.load(scope).get("wake_word.consent") is True
    assert config.load(scope).get("wake_word.enabled") is True
    status = asyncio.run(rpc.status(ctx, WakeNoParams()))
    assert status.listening and status.owner_surface == "gui" and status.available

    loud = struct.pack("<h", 900) + b"\x00" * (FRAME_LENGTH * 2 - 2)
    quiet = b"\x00" * (FRAME_LENGTH * 2)
    pcm = base64.b64encode(loud * 4 + quiet * REARM_FRAMES).decode()
    fed = asyncio.run(rpc.feed(ctx, WakeFeedParams(pcm=pcm)))
    assert fed.fed
    assert events == [(ctx, "wake.detected", WakeDetected(phrase="hey indus", surface="gui"))]
    assert asyncio.run(rpc.resume(ctx, WakeNoParams())).ok
    stopped = asyncio.run(rpc.stop(ctx, WakeStopParams(persist=True)))
    assert stopped.ok and config.load(scope).get("wake_word.enabled") is False
    assert mic.opened == 0


def test_an_on_that_cannot_arm_stores_nothing(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wake_word, "_module_present", lambda name: False)
    rpc, _events, _mic = make(scope)
    answer = asyncio.run(rpc.start(Ctx(scope), WakeStartParams(consent=True)))
    assert answer.reason == "unavailable" and "not installed" in (answer.hint or "")
    assert config.load(scope).get("wake_word.enabled") is False


def test_other_connections_are_not_owners(scope: Scope, model: Path) -> None:
    rpc, _events, _mic = make(scope)
    first, second = Ctx(scope), Ctx(scope)
    asyncio.run(rpc.start(first, WakeStartParams(consent=True, client_capture=True)))
    refused = asyncio.run(rpc.start(second, WakeStartParams(surface="tui", client_capture=True)))
    assert refused.reason == "owned"
    assert asyncio.run(rpc.pause(second, WakeNoParams())).reason == "not_owner"
    rpc.connection_closed(first)
    assert asyncio.run(rpc.start(second, WakeStartParams(client_capture=True))).started


def test_feed_checks(scope: Scope, model: Path) -> None:
    rpc, _events, _mic = make(scope)
    ctx = Ctx(scope)
    with pytest.raises(ProtocolError, match="16 kHz"):
        asyncio.run(rpc.feed(ctx, WakeFeedParams(pcm="AAAA", sample_rate=8000)))
    with pytest.raises(ProtocolError, match="base64"):
        asyncio.run(rpc.feed(ctx, WakeFeedParams(pcm="not base64!")))
    assert not asyncio.run(rpc.feed(ctx, WakeFeedParams(pcm="AAAA"))).fed
