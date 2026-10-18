"""Fake tools for the turn-loop tests (P04-13).

``FakeTools`` registers a small set of async tools on a ``ToolRegistry`` and records what they did:

- ``echo``: returns its arguments as text;
- ``slow``: sleeps ``seconds`` on the injected clock, then answers (records start/finish order);
- ``boom``: raises;
- ``effect``: appends a side-effect marker to ``effects`` (and to an optional callback);
- ``reader``: read-only and path-scoped (``path``); ``writer``: path-scoped writer, mutating;
- ``lookup``: read-only, not path-scoped; returns a fixed text;
- ``human``: waits ``seconds`` as if a person were answering an approval (the ``FakeBroker``
  counts that time as human wait);
- ``big``: returns ``size`` characters.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from indus.foundation.clock import Clock
from indus.protocol.events import CancelReason
from indus.tools.registry import ToolRegistry, ToolTraits


def _schema(**props: Any) -> dict[str, Any]:
    return {"description": "fake tool", "parameters": {"type": "object", "properties": props}}


@dataclass
class FakeBroker:
    """A server-request port stand-in: counts human wait time and cancellations."""

    clock: Clock
    opened_at: float | None = None
    waited: float = 0.0
    cancelled: list[CancelReason] = field(default_factory=list[CancelReason])
    open_requests: int = 0

    def open(self) -> None:
        self.opened_at = self.clock.monotonic()

    def close(self) -> None:
        if self.opened_at is not None:
            self.waited += self.clock.monotonic() - self.opened_at
            self.opened_at = None

    def human_wait_seconds(self) -> float:
        live = self.clock.monotonic() - self.opened_at if self.opened_at is not None else 0.0
        return self.waited + live

    def cancel_all(self, reason: CancelReason) -> int:
        self.cancelled.append(reason)
        count, self.open_requests = self.open_requests, 0
        return count

    async def ask(self, kind: str, payload: Mapping[str, Any], **_: Any) -> Any:
        raise NotImplementedError


@dataclass
class FakeTools:
    clock: Clock
    broker: FakeBroker | None = None
    effects: list[str] = field(default_factory=list[str])
    events: list[str] = field(default_factory=list[str])
    on_effect: Callable[[str], None] | None = None
    running: int = 0
    max_running: int = 0

    def register(self, registry: ToolRegistry) -> ToolRegistry:
        registry.register("echo", "fake", _schema(text={"type": "string"}), self.echo)
        registry.register(
            "slow",
            "fake",
            _schema(seconds={"type": "number"}),
            self.slow,
            traits=ToolTraits(read_only=True),
        )
        registry.register("boom", "fake", _schema(), self.boom)
        registry.register(
            "effect",
            "fake",
            _schema(tag={"type": "string"}),
            self.effect,
            traits=ToolTraits(mutating=True),
        )
        registry.register(
            "reader",
            "fake",
            _schema(path={"type": "string"}),
            self.reader,
            traits=ToolTraits(read_only=True, path_arg="path"),
        )
        registry.register(
            "writer",
            "fake",
            _schema(path={"type": "string"}),
            self.writer,
            traits=ToolTraits(path_arg="path", writer=True, mutating=True),
        )
        registry.register(
            "lookup",
            "fake",
            _schema(q={"type": "string"}),
            self.lookup,
            traits=ToolTraits(read_only=True),
        )
        registry.register(
            "human",
            "fake",
            _schema(seconds={"type": "number"}),
            self.human,
            traits=ToolTraits(read_only=True),
        )
        registry.register(
            "big",
            "fake",
            _schema(size={"type": "integer"}),
            self.big,
            traits=ToolTraits(read_only=True),
        )
        registry.register(
            "web_search",
            "web",
            _schema(q={"type": "string"}),
            self.web,
            traits=ToolTraits(read_only=True),
        )
        return registry

    async def echo(self, args: Mapping[str, Any]) -> str:
        return f"echo {dict(args)}"

    async def slow(self, args: Mapping[str, Any]) -> str:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.events.append(f"start:{args.get('tag', '')}")
        try:
            await self.clock.sleep(float(args.get("seconds", 1)))
        finally:
            self.running -= 1
        self.events.append(f"end:{args.get('tag', '')}")
        return f"slept {args.get('seconds', 1)}"

    async def boom(self, args: Mapping[str, Any]) -> str:
        raise RuntimeError("the fake tool broke")

    async def effect(self, args: Mapping[str, Any]) -> str:
        marker = f"effect:{args.get('tag', '')}"
        self.effects.append(marker)
        if self.on_effect is not None:
            self.on_effect(marker)
        return "done"

    async def reader(self, args: Mapping[str, Any]) -> str:
        return f"read {args.get('path')}"

    async def writer(self, args: Mapping[str, Any]) -> str:
        self.effects.append(f"write:{args.get('path')}")
        return "written"

    async def lookup(self, args: Mapping[str, Any]) -> str:
        return "same answer " * 60

    async def human(self, args: Mapping[str, Any]) -> str:
        seconds = float(args.get("seconds", 300))
        if self.broker is not None:
            self.broker.open()
        try:
            await self.clock.sleep(seconds)
        finally:
            if self.broker is not None:
                self.broker.close()
        await self.clock.sleep(float(args.get("work", 200)))
        return "approved and done"

    async def big(self, args: Mapping[str, Any]) -> str:
        return "x" * int(args.get("size", 10))

    async def web(self, args: Mapping[str, Any]) -> str:
        return f"results for {args.get('q')} " + "lorem ipsum " * 5
