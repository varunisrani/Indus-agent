"""The external memory provider API and manager (P13-24)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.learning.memory import providers
from indus.learning.memory.providers import (
    BaseMemoryProvider,
    DrainReport,
    MemoryWrite,
    ProviderManager,
    ProviderScope,
    RecallStatus,
)
from indus.learning.memory.store import MemoryStore
from indus.learning.memory.tool import MemoryTool
from indus.learning.pending import PendingStore
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.unit.learning._helpers_a import with_config


def _secret() -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "sk-" + "A1b2C3d4" * 5


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    providers.clear_registry()
    yield
    providers.clear_registry()


class FakeProvider(BaseMemoryProvider):
    """A memory provider that records every hand-off so tests can inspect it."""

    def __init__(
        self,
        name: str = "fake",
        *,
        available: bool = True,
        reason: str = "",
        block: threading.Event | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._reason = reason
        self._block = block
        self.initialized: tuple[str, ProviderScope] | None = None
        self.turns: list[Mapping[str, Any]] = []
        self.writes: list[MemoryWrite] = []
        self.queries: list[str] = []
        self.tool_calls: list[tuple[str, Mapping[str, Any]]] = []
        self.shut = 0

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str:
        return self._reason

    def initialize(self, session_id: str, scope: ProviderScope) -> None:
        self.initialized = (session_id, scope)

    def system_prompt_block(self) -> str:
        return "<provider-memory>known: the user likes tea</provider-memory>"

    def prefetch(self, query: str) -> str:
        self.queries.append(query)
        if self._block is not None:
            self._block.wait(10)
        return f"recall for {query}"

    def recall_status(self) -> RecallStatus | None:
        return RecallStatus("Fake", count=1)

    def queue_prefetch(self, query: str) -> None:
        self.queries.append(f"queued:{query}")

    def sync_turn(self, turn: Mapping[str, Any]) -> None:
        if self._block is not None:
            self._block.wait(10)
        self.turns.append(turn)

    def tool_schemas(self) -> Sequence[Mapping[str, Any]]:
        return [{"name": "fake_recall", "description": "search", "parameters": {"type": "object"}}]

    def handle_tool_call(self, name: str, args: Mapping[str, Any]) -> str:
        self.tool_calls.append((name, args))
        return "{}"

    def on_memory_write(self, write: MemoryWrite) -> None:
        self.writes.append(write)

    def shutdown(self) -> None:
        self.shut += 1


def _cfg(scope: Scope, **values: Any) -> Scope:
    return with_config(scope, {"memory": values})


# -- registry and resolution ----------------------------------------------------------------------


def test_only_first_named_provider_is_active(scope: Scope) -> None:
    first = FakeProvider("one")
    second = FakeProvider("two")
    providers.register_provider("one", lambda: first)
    providers.register_provider("one", lambda: second)  # second wins never
    providers.register_provider("two", lambda: second)
    resolved = providers.resolve_provider("one")
    assert resolved is first
    # memory.provider names exactly one; the manager runs that one only.
    manager = ProviderManager.for_scope(_cfg(scope, provider="two"))
    assert manager.provider is second


def test_missing_provider_warns_once_and_runs_builtin_only(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        manager = ProviderManager.for_scope(_cfg(scope, provider="ghost"))
    assert manager.provider is None
    assert manager.start("s1") is False
    assert sum("ghost" in r.message for r in caplog.records) == 1


def test_core_names_mean_builtin_only(scope: Scope) -> None:
    for name in ("", "builtin", "none", "default"):
        assert ProviderManager.for_scope(_cfg(scope, provider=name)).provider is None
    with pytest.raises(ValueError):
        providers.register_provider("builtin", lambda: FakeProvider())


def test_unavailable_provider_warns_and_stays_inert(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    provider = FakeProvider(available=False, reason="no API key")
    manager = ProviderManager(scope=scope, provider=provider)
    with caplog.at_level("WARNING"):
        assert manager.start("s1") is False
    assert not manager.active
    assert any("no API key" in r.message for r in caplog.records)


# -- prompt block ----------------------------------------------------------------------------------


def test_provider_block_frozen_across_turns(scope: Scope) -> None:
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")
    first = manager.prompt_block()
    assert "likes tea" in first
    # Even if the provider would render differently later, the block is frozen.
    provider.system_prompt_block = lambda: "DIFFERENT"  # type: ignore[method-assign]
    assert manager.prompt_block() == first


def test_frozen_block_restored_from_stored_prompt(scope: Scope) -> None:
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")
    manager.freeze_block("<provider-memory>restored bytes</provider-memory>")
    assert manager.prompt_block() == "<provider-memory>restored bytes</provider-memory>"


# -- tools -----------------------------------------------------------------------------------------


def test_tools_hidden_without_memory_toolset(scope: Scope) -> None:
    provider = FakeProvider()
    on = ProviderManager(scope=scope, provider=provider, toolset_selected=True)
    on.start("s1")
    assert [s["name"] for s in on.tool_schemas()] == ["fake_recall"]
    off = ProviderManager(scope=scope, provider=FakeProvider(), toolset_selected=False)
    off.start("s1")
    assert off.tool_schemas() == ()
    assert off.handles_tool("fake_recall") is False


def test_tool_args_reach_provider_redacted(scope: Scope) -> None:
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")
    secret = _secret()
    out = asyncio.run(manager.handle_tool_call("fake_recall", {"q": f"token {secret}"}))
    assert out == "{}"
    name, args = provider.tool_calls[0]
    assert name == "fake_recall"
    assert secret not in args["q"]


# -- prefetch and the 8 s bound --------------------------------------------------------------------


def test_prefetch_returns_recall_and_indicator(scope: Scope) -> None:
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")
    recall = asyncio.run(manager.before_turn(1, "what do you know?"))
    assert "recall for what do you know?" in recall
    assert "recalled 1 memory" in manager.recall_indicator()


def test_trivial_query_skips_prefetch(scope: Scope) -> None:
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")
    assert asyncio.run(manager.before_turn(1, "/help")) == ""
    assert provider.queries == []


def test_prefetch_secret_reaches_provider_redacted(scope: Scope) -> None:
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")
    secret = _secret()
    asyncio.run(manager.before_turn(1, f"my key is {secret}"))
    assert all(secret not in q for q in provider.queries)


def test_slow_prefetch_bounded_by_eight_seconds(scope: Scope) -> None:
    clock = FakeClock(_START)
    clock.allow_threaded_advance()
    block = threading.Event()
    provider = FakeProvider(block=block)
    scoped = scope_with_clock(scope, clock)
    manager = ProviderManager(scope=scoped, provider=provider)
    manager.start("s1")

    async def scenario() -> str:
        task = asyncio.ensure_future(manager.before_turn(1, "slow one"))
        # Drive virtual time past the 8 s deadline (arming it may lag under load), then the
        # awaiting side gives up on the still-blocked prefetch.
        for _ in range(200):
            await _wait_for_sleeper(clock)
            await clock.advance_and_settle(providers.PREFETCH_WAIT_S)
            if task.done():
                break
        return await task

    try:
        recall = asyncio.run(scenario())
    finally:
        block.set()
    assert recall == ""  # the turn did not wait past 8 s
    # A later turn skips prefetch while the stuck call is still running.
    assert asyncio.run(manager.before_turn(2, "next")) == ""


# -- recall ceiling and opt-in spill ---------------------------------------------------------------


def test_recall_ceiling_caps_oversize_recall(scope: Scope) -> None:
    provider = FakeProvider()
    huge = "x" * (providers.RECALL_CEILING + 5000)
    provider.prefetch = lambda query: huge  # type: ignore[method-assign]
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")
    recall = asyncio.run(manager.before_turn(1, "give me everything"))
    assert len(recall) == providers.RECALL_CEILING


def test_opt_in_spill_replaces_oversize_recall(scope: Scope) -> None:
    provider = FakeProvider()
    huge = "y" * (providers.RESULT_CHARS + 5000)
    provider.prefetch = lambda query: huge  # type: ignore[method-assign]
    manager = ProviderManager(scope=_cfg(scope, provider_recall_spill=True), provider=provider)
    manager.start("s1")
    recall = asyncio.run(manager.before_turn(1, "give me everything"))
    assert "<spilled-result>" in recall
    assert len(recall) < len(huge)


# -- sync, mirror, delegation ----------------------------------------------------------------------


def test_after_turn_syncs_and_queues_next(scope: Scope) -> None:
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")

    async def scenario() -> None:
        secret = _secret()
        manager.after_turn({"user": f"ask {secret}", "assistant": "ok"}, next_query="what next")
        await manager.shutdown()

    asyncio.run(scenario())
    assert len(provider.turns) == 1
    assert _secret() not in str(provider.turns[0])
    assert "queued:what next" in provider.queries


def test_builtin_add_mirrored_once(scope: Scope) -> None:
    store = MemoryStore.for_scope(_cfg(scope, notes_enabled=True, profile_enabled=True))
    tool = MemoryTool(store, PendingStore(scope.home, scope.clock))
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")

    async def scenario() -> None:
        args = {"action": "add", "target": "memory", "content": "uses uv"}
        result = await tool.run(args, turn_id="t1")
        manager.notify_tool_write(result, args)
        await manager.shutdown()

    asyncio.run(scenario())
    assert [w.action for w in provider.writes] == ["add"]
    assert provider.writes[0].content == "uses uv"


def test_patch_mirrored_as_changed_entry(scope: Scope) -> None:
    store = MemoryStore.for_scope(_cfg(scope))
    tool = MemoryTool(store, PendingStore(scope.home, scope.clock))
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")

    async def scenario() -> None:
        await tool.run({"action": "add", "target": "memory", "content": "uses pip"}, turn_id="t1")
        args = {"action": "patch", "target": "memory", "old_text": "pip", "content": "uv"}
        result = await tool.run(args, turn_id="t2")
        manager.notify_tool_write(result, args)
        await manager.shutdown()

    asyncio.run(scenario())
    assert [w.action for w in provider.writes] == ["patch"]
    assert provider.writes[0].content == "uses uv"
    assert provider.writes[0].previous == "uses pip"


def test_staged_write_is_not_mirrored(scope: Scope) -> None:
    # A gated write stages instead of committing; nothing is mirrored.
    store = MemoryStore.for_scope(_cfg(scope, write_approval=True))
    tool = MemoryTool(store, PendingStore(scope.home, scope.clock), gate=lambda: True)
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")

    async def scenario() -> None:
        args = {"action": "add", "target": "memory", "content": "held back"}
        result = await tool.run(args, turn_id="t1")
        assert result.get("staged") is True
        manager.notify_tool_write(result, args)
        await manager.shutdown()

    asyncio.run(scenario())
    assert provider.writes == []


def test_delegation_result_redacted(scope: Scope) -> None:
    provider = FakeProvider()
    captured: list[tuple[str, str]] = []
    provider.on_delegation = lambda task, result, child_session_id="": captured.append(  # type: ignore[method-assign]
        (task, result)
    )
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")

    async def scenario() -> None:
        manager.delegation("do it", f"result with {_secret()}", "child-1")
        await manager.shutdown()

    asyncio.run(scenario())
    assert captured and _secret() not in captured[0][1]


# -- shutdown drain --------------------------------------------------------------------------------


def test_shutdown_waits_at_most_five_seconds(scope: Scope) -> None:
    clock = FakeClock(_START)
    clock.allow_threaded_advance()
    block = threading.Event()
    provider = FakeProvider(block=block)
    scoped = scope_with_clock(scope, clock)
    manager = ProviderManager(scope=scoped, provider=provider)
    manager.start("s1")

    async def scenario() -> DrainReport:
        manager.after_turn({"user": "hi", "assistant": "there"})
        task = asyncio.ensure_future(manager.shutdown())
        # Wait until the drain has armed its 5 s deadline on the clock.
        await _wait_for_sleeper(clock)
        await clock.advance_and_settle(providers.DRAIN_WAIT_S)
        return await task

    try:
        report = asyncio.run(scenario())
    finally:
        block.set()
    assert report.timed_out is True
    assert report.abandoned_writes == 1
    assert report.abandoned_prefetches == 0


def test_clean_shutdown_drains_and_closes_provider(scope: Scope) -> None:
    provider = FakeProvider()
    manager = ProviderManager(scope=scope, provider=provider)
    manager.start("s1")

    async def scenario() -> DrainReport:
        manager.after_turn({"user": "hi", "assistant": "there"})
        return await manager.shutdown()

    report = asyncio.run(scenario())
    assert report == DrainReport()
    assert provider.shut == 1
    assert len(provider.turns) == 1


# -- helpers ---------------------------------------------------------------------------------------

_START = datetime(2026, 1, 1, tzinfo=UTC)


def scope_with_clock(scope: Scope, clock: FakeClock) -> Scope:
    return replace(scope, clock=clock)


async def _wait_for_sleeper(clock: FakeClock) -> None:
    """Wait until a task has parked on ``clock`` (its deadline timer is armed).

    The timer is armed only after ``on_turn_start`` returns from a worker thread, so this waits on
    real time (bounded), not on a number of loop turns a loaded machine can use up first.
    """
    try:
        await eventually(lambda: clock.pending >= 1, limit=10.0)
    except AssertionError:
        raise AssertionError("no clock sleeper appeared") from None
