"""P24-07: ``message_agent`` — typed failures, one retry when it can help, the bots-only toolset."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.state.bot_chat import FAILURE_REASONS, RETRYABLE_REASONS, FailureReason
from indus.tools import toolsets
from indus.tools.builtin import message_agent as mod
from indus.tools.builtin.message_agent import (
    MAX_MESSAGE_CHARS,
    MAX_REPLY_CHARS,
    TOOL_NAME,
    BotDelivery,
    BotTarget,
    DeliveryOutcome,
    install_transport,
    message_agent,
    parse_target,
    register_tools,
)
from indus.tools.registry import ToolRegistry


@dataclass
class Scripted:
    """A transport that answers from a script and records what it was asked."""

    answers: list[DeliveryOutcome | Exception]
    seen: list[BotDelivery] = field(default_factory=list[BotDelivery])

    async def deliver(self, delivery: BotDelivery) -> DeliveryOutcome:
        self.seen.append(delivery)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _send(scope: Scope, transport: Scripted | None, **args: Any) -> dict[str, Any]:
    body = {"target": "bob", "message": "can you review the plan?", **args}
    return json.loads(asyncio.run(message_agent(body, scope=scope, bot_transport=transport)))


@pytest.fixture(autouse=True)
def _fresh_memo() -> Iterator[None]:
    toolsets.clear_memo()
    yield
    toolsets.clear_memo()


def test_delivered_reply_and_attribution(scope: Scope) -> None:
    transport = Scripted([DeliveryOutcome.delivered("looks good")])
    out = _send(scope, transport)
    assert out == {"status": "delivered", "target": "bob", "reply": "looks good", "attempts": 1}
    (seen,) = transport.seen
    assert seen.target == BotTarget("bob")
    assert seen.message.startswith(f"Message from bot @{scope.profile_id}:\n\n")
    assert seen.message.endswith("can you review the plan?")


@pytest.mark.parametrize("reason", sorted(RETRYABLE_REASONS))
def test_retryable_failure_is_retried_once(scope: Scope, reason: FailureReason) -> None:
    transport = Scripted([DeliveryOutcome.failed(reason, "first"), DeliveryOutcome.delivered("ok")])
    out = _send(scope, transport)
    assert (out["status"], out["attempts"]) == ("delivered", 2)
    assert [d.attempt for d in transport.seen] == [1, 2]


def test_retry_happens_at_most_once(scope: Scope) -> None:
    transport = Scripted(
        [
            DeliveryOutcome.failed("runtime_offline", "down"),
            DeliveryOutcome.failed("runtime_offline", "still down"),
            DeliveryOutcome.delivered("never asked"),
        ]
    )
    out = _send(scope, transport)
    assert (out["reason"], out["retried"], out["retryable"]) == ("runtime_offline", True, True)
    assert out["error"] == "still down"
    assert len(transport.seen) == 2


@pytest.mark.parametrize("reason", sorted(set(FAILURE_REASONS) - set(RETRYABLE_REASONS)))
def test_non_retryable_failure_is_not_retried(scope: Scope, reason: FailureReason) -> None:
    transport = Scripted([DeliveryOutcome.failed(reason, "no"), DeliveryOutcome.delivered("x")])
    out = _send(scope, transport)
    assert (out["reason"], out["error_type"], out["retried"]) == (reason, reason, False)
    assert len(transport.seen) == 1


def test_a_transport_crash_comes_back_typed(scope: Scope) -> None:
    fake_key = "sk-" + "x" * 24
    transport = Scripted([RuntimeError(f"401 unauthorized with key {fake_key}")])
    out = _send(scope, transport)
    assert out["reason"] == "provider_auth"
    assert fake_key not in json.dumps(out)


def test_bad_inputs_are_refused_without_delivery(scope: Scope) -> None:
    transport = Scripted([])
    assert "error" in _send(scope, transport, target="")
    assert "error" in _send(scope, transport, message="   ")
    assert "error" in _send(scope, transport, message="x" * (MAX_MESSAGE_CHARS + 1))
    assert "error" in _send(scope, transport, target="bad name!")
    assert "itself" in _send(scope, transport, target=scope.profile_id)["error"]
    assert transport.seen == []


def test_no_transport_means_runtime_offline(scope: Scope) -> None:
    out = _send(scope, None)
    assert (out["reason"], out["retried"]) == ("runtime_offline", False)


def test_an_installed_transport_is_used_for_its_profile(scope: Scope) -> None:
    transport = Scripted([DeliveryOutcome.delivered("hi")])
    undo = install_transport(scope, transport)
    try:
        out = json.loads(asyncio.run(message_agent({"target": "bob", "message": "m"}, scope=scope)))
    finally:
        undo()
    assert out["status"] == "delivered"
    assert mod.transport_for(scope) is None


def test_long_replies_are_cut_with_a_note(scope: Scope) -> None:
    transport = Scripted([DeliveryOutcome.delivered("r" * (MAX_REPLY_CHARS + 50))])
    out = _send(scope, transport)
    assert out["reply"].startswith("r" * MAX_REPLY_CHARS)
    assert "50 more characters" in out["reply"]


def test_a_silent_reply_is_noted(scope: Scope) -> None:
    out = _send(scope, Scripted([DeliveryOutcome.delivered("")]))
    assert out["reply"] == "" and "chose not to reply" in out["note"]


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("bob", BotTarget("bob")),
        ("@Bob", BotTarget("bob")),
        ("spark/researcher", BotTarget("spark", "researcher")),
        (" spark / researcher ", BotTarget("spark", "researcher")),
    ],
)
def test_target_forms(text: str, target: BotTarget) -> None:
    assert parse_target(text) == target


def test_the_tool_lives_only_in_the_bots_bundle() -> None:
    registry = ToolRegistry()
    register_tools(registry)
    assert registry.get(TOOL_NAME) is not None
    everything = toolsets.select(None, registry=registry, platform="cli").tools
    assert TOOL_NAME not in everything  # "all" never brings it in
    assert TOOL_NAME not in toolsets.select(["indus-bots"], registry=registry, platform="cli").tools
    bots = toolsets.select(["indus-bots"], registry=registry, platform="bots").tools
    assert TOOL_NAME in bots and "terminal" in bots
    assert TOOL_NAME not in toolsets.resolve("indus-gateway", registry)
