"""``indus send`` to the P16 E6–E8 chat platforms through the real ``send_command.build_adapter``:
the built-in registry row (its lazy factory and the package's own ``create_adapter``), the
credentials read by the gateway's enablement pass, a send-only adapter that does no inbound work.
Only the network is swapped: the package's ``create_adapter`` is wrapped to pass the wire fake's
transport (and socket opener)."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable
from dataclasses import replace
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter
from indus.gateway.config import PlatformSettings, load_gateway_settings
from indus.gateway.core.send import SendResult
from indus.gateway.delivery import send_command
from indus.gateway.registry import PlatformRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform


async def _drive[T](clock: FakeClock, awaitable: Awaitable[T]) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(2000):
        await clock.run_until_idle()
        await asyncio.wait({task}, timeout=0.001)
        if task.done():
            return task.result()
        clock.advance(0.5)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


def _settings(scope: Scope, name: str) -> PlatformSettings:
    found = load_gateway_settings(scope).platforms[name]
    assert found.enabled and found.reason == "credentials", found.reason
    return found


def _registry(name: str, *, socket_extra: bool = False) -> PlatformRegistry:
    """The built-in registry. ``socket_extra``: the row's probe names httpx-ws (the socket), which
    a send does not use and the gate does not install, so only the probe is forced (the entry's
    factory, lock key and everything else stay the built-in row's)."""
    registry = PlatformRegistry()
    if socket_extra:
        entry = registry.get(name)
        assert entry is not None and entry.check_fn() in (True, False)
        registry.register(replace(entry, check_fn=lambda: True), replace_existing=True)
    return registry


def _wrap(monkeypatch: pytest.MonkeyPatch, package: Any, **network: Any) -> list[Adapter]:
    built: list[Adapter] = []
    real = package.create_adapter

    def create(config: Any) -> Adapter:
        adapter = functools.partial(real, **network)(config)
        built.append(adapter)
        return adapter

    monkeypatch.setattr(package, "create_adapter", create)
    return built


def _send(scope: Scope, adapter: Adapter | None, chat: str, text: str) -> SendResult:
    assert adapter is not None
    clock: Any = scope.clock

    async def main() -> SendResult:
        await _drive(clock, adapter.connect())
        result = await _drive(clock, adapter.send(chat, text))
        await _drive(clock, adapter.disconnect())
        return result

    return asyncio.run(main())


def test_indus_send_to_teams(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import teams as package
    from indus.gateway.adapters.teams.adapter import TeamsAdapter
    from tests.fakes.platform.teams_wire import APP_ID, MAX_LEN, TENANT_ID, TeamsWire

    clock: Any = scope.clock
    platform = FakeChatPlatform("teams", clock=clock, max_len=MAX_LEN)
    wire = TeamsWire(platform)
    secrets: Any = scope.secrets
    secrets.set("TEAMS_CLIENT_ID", APP_ID)
    secrets.set("TEAMS_CLIENT_SECRET", wire.secret)
    secrets.set("TEAMS_TENANT_ID", TENANT_ID)
    built = _wrap(monkeypatch, package, transport=wire.transport())
    adapter = send_command.build_adapter(
        scope, "teams", _settings(scope, "teams"), _registry("teams")
    )
    assert isinstance(adapter, TeamsAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, "a1conversation", "deploy done").ok
    assert platform.texts("a1conversation") == ["deploy done"]
    assert [op.kind for op in platform.ops] == ["connect", "send", "disconnect"]
    assert not adapter.listening and wire.key_requests == 0


def test_indus_send_to_google_chat(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import google_chat as package
    from indus.gateway.adapters.google_chat.adapter import GoogleChatAdapter
    from tests.fakes.platform.google_chat_wire import MAX_LEN, GoogleChatWire

    clock: Any = scope.clock
    platform = FakeChatPlatform("google_chat", clock=clock, max_len=MAX_LEN)
    wire = GoogleChatWire(platform)
    secrets: Any = scope.secrets
    secrets.set("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", wire.account_json)
    built = _wrap(monkeypatch, package, transport=wire.transport())
    adapter = send_command.build_adapter(
        scope, "google_chat", _settings(scope, "google_chat"), _registry("google_chat")
    )
    assert isinstance(adapter, GoogleChatAdapter) and adapter is built[0] and adapter.send_only
    assert adapter.listener is None
    assert _send(scope, adapter, "spaces/G300", "deploy done").ok
    assert platform.texts("spaces/G300") == ["deploy done"]
    assert [op.kind for op in platform.ops] == ["connect", "send", "disconnect"]
    assert not any(":pull" in call.path for call in wire.calls)


def test_indus_send_to_qqbot(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import qqbot as package
    from indus.gateway.adapters.qqbot.adapter import QQBotAdapter
    from tests.fakes.platform.qqbot_wire import MAX_LEN, QQWire

    clock: Any = scope.clock
    platform = FakeChatPlatform("qqbot", clock=clock, max_len=MAX_LEN)
    wire = QQWire(platform)
    secrets: Any = scope.secrets
    secrets.set("QQ_APP_ID", wire.app_id)
    secrets.set("QQ_CLIENT_SECRET", wire.secret)
    built = _wrap(monkeypatch, package, transport=wire.transport(), opener=wire.opener)
    registry = _registry("qqbot", socket_extra=True)
    adapter = send_command.build_adapter(scope, "qqbot", _settings(scope, "qqbot"), registry)
    assert isinstance(adapter, QQBotAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, "group:G1", "deploy done").ok
    assert platform.texts("group:G1") == ["deploy done"]
    assert wire.socket_urls == [] and not wire.calls_to("GET", "/gateway")


def test_indus_send_to_dingtalk(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import dingtalk as package
    from indus.gateway.adapters.dingtalk.adapter import DingTalkAdapter
    from tests.fakes.platform.dingtalk_wire import DingTalkWire

    clock: Any = scope.clock
    wire = DingTalkWire(clock=clock)
    secrets: Any = scope.secrets
    secrets.set("DINGTALK_CLIENT_ID", wire.client_id)
    secrets.set("DINGTALK_CLIENT_SECRET", wire.secret)
    built = _wrap(monkeypatch, package, transport=wire.transport(), opener=wire.open)
    registry = _registry("dingtalk", socket_extra=True)
    adapter = send_command.build_adapter(scope, "dingtalk", _settings(scope, "dingtalk"), registry)
    assert isinstance(adapter, DingTalkAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, "user:staff-ana", "deploy done").ok
    assert wire.platform.texts("staff-ana") == ["deploy done"] and wire.tickets == []


def test_indus_send_to_wecom(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import wecom as package
    from indus.gateway.adapters.wecom.adapter import WeComAdapter
    from tests.fakes.platform.wecom_wire import WeComWire

    clock: Any = scope.clock
    wire = WeComWire(clock=clock)
    secrets: Any = scope.secrets
    secrets.set("WECOM_BOT_ID", wire.bot_id)
    secrets.set("WECOM_SECRET", wire.secret)
    built = _wrap(monkeypatch, package, transport=wire.transport(), opener=wire.open)
    registry = _registry("wecom", socket_extra=True)
    adapter = send_command.build_adapter(scope, "wecom", _settings(scope, "wecom"), registry)
    assert isinstance(adapter, WeComAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, "wrGroup01", "deploy done").ok
    assert wire.platform.texts("wrGroup01") == ["deploy done"]
