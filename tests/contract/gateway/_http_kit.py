"""The gateway's HTTP surfaces wired to the fake model, for the P15-30..32 contract tests.

``build_api`` makes a real ``SessionService`` (fake model, a real store in the test's ``work``
profile, ``answerable_kinds`` approval + clarify as the daemon's ``API_SURFACE`` asks), a
``SessionDesk`` over it, an ``ApiKeyRing`` over an in-memory secret store with one key, and the
whole ASGI app; requests go through httpx's ASGI transport (no socket). ``build_webhook`` does the
same for the receiver with a recording delivery port.

Inline tools: ``flagged`` runs the real P05 approval decision for a dangerous command in the
turn's run context (``unattended`` denies at once, ``interactive`` asks through the broker).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from indus.core.persist import StateStore
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.gateway.http.api_server import ApiContext, ApiSettings, build_api_app
from indus.gateway.http.serving import Asgi
from indus.gateway.http.turns import FileCompletionLog, SessionDesk
from indus.gateway.http.webhook import DeliveryOutcome, WebhookReceiver, WebhookSettings
from indus.safety.approvals import ApprovalContext, ApprovalState, decide
from indus.service.session_service import SessionService
from indus.state import registry as store_registry
from indus.state.api_keys import ApiKeyRing
from indus.state.registry import StoreHandle
from indus.tools.calls import ToolCall, ToolResult
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, Turn, fake_route, script_of
from tests.fakes.secret_store import MemorySecretStore

DANGEROUS = "git push --force origin main"


def flagged_tool(scope: Scope) -> Callable[[ToolCall, TurnState], Awaitable[ToolResult]]:
    """An inline tool that runs the real approval decision for ``DANGEROUS``."""

    async def run(call: ToolCall, state: TurnState) -> ToolResult:
        ctx = ApprovalContext(
            scope=scope,
            session_id=state.runtime.session_id,
            turn_id=state.turn_id,
            run_context=state.runtime.run_context or "interactive",  # pyright: ignore[reportArgumentType]
            broker=state.control.broker,
            state=ApprovalState(),
        )
        decision = await decide(DANGEROUS, ctx)
        word = "ran" if decision.approved else "blocked"
        return ToolResult(f"{word} ({decision.outcome}): {decision.message or ''}")

    return run


@dataclass
class ApiKit:
    scope: Scope
    clock: FakeClock
    server: FakeModelServer
    service: SessionService
    store: StoreHandle
    desk: SessionDesk
    ctx: ApiContext
    app: Asgi
    key: str
    secrets: MemorySecretStore
    knobs: dict[str, Any]

    def client(self, key: str | None = "default", **kw: Any) -> httpx.AsyncClient:
        headers: dict[str, str] = dict(kw.pop("headers", {}))
        token = self.key if key == "default" else key
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 50000))  # pyright: ignore[reportArgumentType]
        return httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8642", headers=headers, **kw
        )

    def model_texts(self) -> list[str]:
        """The user messages of every model request (last one each)."""
        found: list[str] = []
        for request in self.server.model_requests:
            body = request.body if isinstance(request.body, dict) else {}
            users = [m for m in body.get("messages") or [] if m.get("role") == "user"]
            if users:
                content = users[-1].get("content")
                found.append(content if isinstance(content, str) else json.dumps(content))
        return found

    def model_messages(self, index: int = -1) -> list[dict[str, Any]]:
        body = self.server.model_requests[index].body
        return list(body.get("messages") or []) if isinstance(body, dict) else []

    async def close(self) -> None:
        await self.ctx.runs.aclose()
        await self.desk.aclose()
        await self.service.aclose()
        self.store.close()


def make_service(
    scope: Scope,
    server: FakeModelServer,
    tmp_path: Path,
    store: StoreHandle,
    *,
    toolsets: list[str],
    kinds: tuple[str, ...],
    inline: Mapping[str, Any] | None = None,
    registry: ToolRegistry | None = None,
    platform: str = "api_server",
) -> SessionService:
    options = fake_options(
        server,
        session_id=None,
        registry=registry,
        inline=dict(inline or {}),
        platform=platform,
        source=platform,
    )
    return SessionService(
        scope,
        fake_route(server),
        toolsets=toolsets,
        options=options,
        store=StateStore(store),
        lock_dir=tmp_path / "locks",
        answerable_kinds=kinds,
        owner=f"gateway-{platform}",
    )


async def build_api(
    scope: Scope,
    clock: FakeClock,
    tmp_path: Path,
    *turns: Turn,
    inline: Mapping[str, Any] | None = None,
    registry: ToolRegistry | None = None,
    settings: Mapping[str, Any] | None = None,
    cron: Any = None,
) -> ApiKit:
    server = FakeModelServer(script_of(*turns), clock=clock)
    store = store_registry.open_unshared(scope)
    tools = {"flagged": flagged_tool(scope), **(inline or {})}
    service = make_service(
        scope,
        server,
        tmp_path,
        store,
        toolsets=["indus-api-server"],
        kinds=("approval", "clarify"),
        inline=tools,
        registry=registry,
    )
    knobs: dict[str, Any] = dict(settings or {})
    completions = FileCompletionLog(scope)
    desk = SessionDesk(
        service,
        clock,
        toolsets=["indus-api-server"],
        completions=completions,
        idle_close_s=float(knobs.get("idle_close_seconds", 900.0)),
    )
    secrets = MemorySecretStore()
    keys = ApiKeyRing(scope, secrets)
    _, key = keys.create("tests")
    ctx = ApiContext(
        scope=scope,
        desk=desk,
        keys=keys,
        settings=lambda: ApiSettings(**knobs),
        clock=clock,
        store=store,
        completions=completions,
        cron=cron,
    )
    app = build_api_app(ctx)
    return ApiKit(scope, clock, server, service, store, desk, ctx, app, key, secrets, knobs)


async def within(awaitable: Awaitable[Any], seconds: float = 20.0) -> Any:
    """Await with a real-time guard so a broken test fails instead of hanging."""
    return await asyncio.wait_for(awaitable, seconds)


async def breathe(seconds: float = 0.005) -> None:
    """Let other tasks and threads run for a moment of real time (no clock involved)."""
    idle = asyncio.get_running_loop().create_future()
    await asyncio.wait({idle}, timeout=seconds)
    idle.cancel()


async def until(check: Callable[[], object], seconds: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not check():
        if loop.time() > end:
            raise AssertionError("the condition never held")
        idle = loop.create_future()
        await asyncio.wait({idle}, timeout=0.002)
        idle.cancel()


def sse_events(text: str) -> list[dict[str, Any]]:
    """Parse an SSE body into frames: ``{"event", "id", "data"}`` (comments kept as
    ``{"comment": ...}``)."""
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, Any] = {}
        data: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":"):
                frames.append({"comment": line[1:].strip()})
            elif line.startswith("data: "):
                data.append(line[6:])
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("id: "):
                frame["id"] = int(line[4:])
        if data:
            raw = "\n".join(data)
            frame["data"] = raw if raw == "[DONE]" else json.loads(raw)
            frames.append(frame)
    return frames


# -- webhook ------------------------------------------------------------------------------------


@dataclass
class Sent:
    target: str
    text: str
    route: str


@dataclass
class RecordingDeliver:
    sent: list[Sent] = field(default_factory=list[Sent])
    fail: bool = False

    async def __call__(self, target: str, text: str, *, route: str) -> DeliveryOutcome:
        self.sent.append(Sent(target, text, route))
        return DeliveryOutcome(not self.fail, "refused" if self.fail else None)


@dataclass
class HookKit:
    scope: Scope
    clock: FakeClock
    server: FakeModelServer
    service: SessionService
    store: StoreHandle
    desk: SessionDesk
    receiver: WebhookReceiver
    deliver: RecordingDeliver
    secrets: MemorySecretStore
    knobs: dict[str, Any]

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.receiver.app, client=("127.0.0.1", 50001))  # pyright: ignore[reportArgumentType]
        return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8644")

    async def close(self) -> None:
        await self.receiver.aclose()
        await self.service.aclose()
        self.store.close()


async def build_webhook(
    scope: Scope,
    clock: FakeClock,
    tmp_path: Path,
    *turns: Turn,
    routes: Mapping[str, Any],
    secrets: Mapping[str, str] | None = None,
    settings: Mapping[str, Any] | None = None,
    registry: ToolRegistry | None = None,
    load: bool = True,
) -> HookKit:
    server = FakeModelServer(script_of(*turns), clock=clock)
    store = store_registry.open_unshared(scope)
    service = make_service(
        scope,
        server,
        tmp_path,
        store,
        toolsets=["indus-webhook"],
        kinds=(),
        registry=registry,
        platform="webhook",
    )
    desk = SessionDesk(service, clock, toolsets=["indus-webhook"])
    vault = MemorySecretStore(dict(secrets or {}))
    deliver = RecordingDeliver()
    knobs: dict[str, Any] = {"routes": dict(routes), **(settings or {})}
    receiver = WebhookReceiver(
        scope,
        settings=lambda: WebhookSettings(**knobs),
        secrets=vault,
        desk=desk,
        deliver=deliver,
        clock=clock,
    )
    if load:
        receiver.load()
    return HookKit(scope, clock, server, service, store, desk, receiver, deliver, vault, knobs)
