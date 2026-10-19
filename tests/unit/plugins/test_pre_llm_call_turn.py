"""``pre_llm_call`` through a real turn: plugin and shell-hook context rides the user message that
is sent (and stored as its ``api_content``), never the system prompt (R07.18, R01.1)."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.core.agent import Agent
from indus.core.persist import MemoryStore
from indus.foundation.blocking import BlockingPool
from indus.foundation.scope import Scope
from indus.plugins.hooks import HookBus
from indus.plugins.session import plugin_options
from indus.plugins.shell_hooks import register_shell_hooks
from tests.fakes.agent import DictConfig, fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn


@pytest.fixture
def pool() -> Iterator[BlockingPool]:
    made = BlockingPool("pre-llm-test", 4)
    yield made
    made.shutdown()


def messages(server: FakeModelServer, index: int) -> list[dict[str, Any]]:
    return server.model_requests[index].body["messages"]


async def two_turns(
    scope: Scope, clock: FakeClock, bus: HookBus | None, store: MemoryStore
) -> tuple[FakeModelServer, Agent]:
    server = FakeModelServer(script_of(text_turn("one"), text_turn("two")), clock=clock)
    options = fake_options(server, store=store)
    if bus is not None:
        options = plugin_options(options, bus, session_id="s1")
    agent = await Agent.start(scope, fake_route(server), [], options, None)
    await run_driven(agent.run_turn("hi"), clock)
    await run_driven(agent.run_turn("again"), clock)
    await agent.close()
    return server, agent


def test_plugin_context_rides_the_user_message(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool
) -> None:
    seen: list[dict[str, Any]] = []

    def context(**payload: Any) -> dict[str, str]:
        seen.append(payload)
        return {"context": f"weather for turn {len(seen)}: sunny"}

    bus = HookBus(clock=fake_clock, pool=pool)
    bus.register("pre_llm_call", context, owner="weather")
    bus.register("pre_llm_call", lambda **_: "second plugin says hello", owner="other")

    async def main() -> tuple[FakeModelServer, FakeModelServer, MemoryStore]:
        store = MemoryStore()
        hooked, _ = await two_turns(scope, fake_clock, bus, store)
        plain, _ = await two_turns(scope, fake_clock, None, MemoryStore())
        return hooked, plain, store

    hooked, plain, store = asyncio.run(main())
    first = messages(hooked, 0)
    assert first[-1] == {
        "role": "user",
        "content": "hi\n\nweather for turn 1: sunny\n\nsecond plugin says hello",
    }
    # The system prompt bytes are exactly those of a session without hooks.
    assert first[0] == messages(plain, 0)[0]
    assert messages(hooked, 1)[0] == messages(plain, 1)[0]
    # Turn two replays turn one's user message with the same bytes (stored api_content).
    second = messages(hooked, 1)
    assert second[1] == first[-1]
    assert second[-1]["content"].startswith("again\n\nweather for turn 2: sunny")
    # What the person typed is stored as typed; the context is the sidecar.
    rows = asyncio.run(store.load("s1"))
    user_rows = [r for r in rows if r.role == "user"]
    assert [r.content for r in user_rows] == ["hi", "again"]
    assert user_rows[0].api_content is not None and "sunny" in user_rows[0].api_content
    # The payload the hook got.
    assert seen[0]["user_message"] == "hi" and seen[0]["is_first_turn"] is True
    assert seen[1]["is_first_turn"] is False and seen[1]["session_id"] == "s1"
    assert [m["role"] for m in seen[1]["conversation_history"]] == ["user", "assistant"]


def test_a_failing_hook_never_fails_the_turn(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool
) -> None:
    def broken(**_: Any) -> str:
        raise RuntimeError("plugin bug")

    bus = HookBus(clock=fake_clock, pool=pool)
    bus.register("pre_llm_call", broken, owner="bad")

    async def main() -> FakeModelServer:
        server, _ = await two_turns(scope, fake_clock, bus, MemoryStore())
        return server

    server = asyncio.run(main())
    assert messages(server, 0)[-1] == {"role": "user", "content": "hi"}


def test_shell_hook_context_through_a_turn(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, tmp_path: Path
) -> None:
    script = tmp_path / "context.py"
    script.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'hookSpecificOutput': {'additionalContext': "
        "'shell saw: ' + payload['prompt']}}))\n",
        encoding="utf-8",
    )
    command = f"{json.dumps(sys.executable)} {json.dumps(str(script))}"
    configured = scope.with_config(DictConfig({"hooks": {"pre_llm_call": [{"command": command}]}}))
    bus = HookBus(clock=None, pool=pool)
    report = register_shell_hooks(bus, configured, interactive=False, accept=True)
    assert len(report.registered) == 1

    async def main() -> FakeModelServer:
        server, _ = await two_turns(configured, fake_clock, bus, MemoryStore())
        return server

    server = asyncio.run(main())
    assert messages(server, 0)[-1]["content"] == "hi\n\nshell saw: hi"
