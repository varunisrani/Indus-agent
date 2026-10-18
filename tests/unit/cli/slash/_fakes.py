"""A fake ``SessionPort`` and helpers for the slash-command tests.

``FakePort`` records every call; ``busy`` stands for "a turn is running" (``wait_idle`` and
``interrupt`` end it). ``switch_route`` replaces the route unless ``fail_switch`` is set.
``make_ctx`` builds a ``SlashContext`` whose output lands in ``ctx.lines``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from indus.cli.slash.context import RouteInfo, SessionPort, SlashContext
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.protocol.methods import SessionCreateParams
from indus.providers.route import RuntimeRoute
from indus.state import messages, sessions
from indus.state.registry import StoreHandle

FAKE_URL = "http://127.0.0.1:9/v1"


def route_of(model: str = "m1", provider: str = "fake", base_url: str = FAKE_URL) -> RuntimeRoute:
    return RuntimeRoute(
        provider=provider, model=model, api_mode="chat_completions", base_url=base_url
    )


@dataclass
class FakePort:
    route_now: RouteInfo = field(
        default_factory=lambda: RouteInfo(route_of(), 128_000, "medium", "medium")
    )
    busy: bool = False
    fail_switch: Exception | None = None
    tools: list[str] = field(default_factory=lambda: ["read_file", "terminal", "web_search"])
    jobs: int = 0
    calls: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])
    created: list[SessionCreateParams] = field(default_factory=list[SessionCreateParams])
    steers: list[str] = field(default_factory=list[str])
    _count: int = 0

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def create(self, params: SessionCreateParams) -> str:
        self._count += 1
        self.created.append(params)
        sid = f"new{self._count}"
        self.calls.append(("create", sid))
        return sid

    def resume(self, session_id: str) -> None:
        self.calls.append(("resume", session_id))

    def close(self, session_id: str) -> None:
        self.calls.append(("close", session_id))

    def interrupt(self, session_id: str) -> bool:
        self.calls.append(("interrupt", session_id))
        return self.busy

    def wait_idle(self, session_id: str, timeout: float) -> bool:
        self.calls.append(("wait_idle", session_id))
        self.busy = False
        return True

    def steer(self, session_id: str, text: str) -> bool:
        self.calls.append(("steer", text))
        self.steers.append(text)
        return self.busy

    def route(self, session_id: str) -> RouteInfo:
        return self.route_now

    def switch_route(
        self,
        session_id: str,
        route: RuntimeRoute,
        *,
        effort: str | None = None,
        keep_effort: bool,
    ) -> RouteInfo:
        self.calls.append(("switch_route", (route.provider, route.model, effort, keep_effort)))
        if self.fail_switch is not None:
            raise self.fail_switch
        wanted = self.route_now.requested_effort if keep_effort else effort
        self.route_now = RouteInfo(route, 200_000, wanted, wanted)
        return self.route_now

    def tool_names(self, session_id: str) -> list[str]:
        return list(self.tools)

    def stop_processes(self, session_id: str, *, all_jobs: bool) -> int:
        self.calls.append(("stop_processes", all_jobs))
        stopped, self.jobs = self.jobs, 0
        return stopped


class Ctx(SlashContext):
    """A ``SlashContext`` that keeps what it printed in ``lines``."""

    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def make_ctx(
    scope: Scope,
    *,
    store: StoreHandle | None = None,
    port: SessionPort | None = None,
    session_id: str = "s1",
    **values: Any,
) -> Ctx:
    lines: list[str] = []
    fake: SessionPort = port if port is not None else FakePort()
    values.setdefault("is_busy", lambda: bool(getattr(fake, "busy", False)))
    values.setdefault("out", lines.append)
    ctx = Ctx(scope=scope, port=fake, session_id=session_id, store=store, **values)
    ctx.lines = lines
    return ctx


def with_config(scope: Scope, text: str) -> Scope:
    """Write ``config.yaml`` and return the scope reading it (as bootstrap would)."""
    path = loader.config_path(scope)
    path.write_text(text, encoding="utf-8")
    loader.invalidate(path)
    return scope.with_config(loader.load(scope))


def seed(store: StoreHandle, session_id: str, turns: int, *, title: str | None = None) -> None:
    """A stored session with ``turns`` user/assistant exchanges (q1/a1, q2/a2, ...)."""
    sessions.create(store, session_id, "cli")
    for n in range(1, turns + 1):
        messages.append(store, session_id, {"role": "user", "content": f"q{n}"})
        messages.append(store, session_id, {"role": "assistant", "content": f"a{n}"})
    if title is not None:
        from indus.state import titles

        titles.set_title(store, session_id, title)


def live_users(store: StoreHandle, session_id: str) -> list[str]:
    return [str(row["content"]) for row in messages.get(store, session_id) if row["role"] == "user"]
