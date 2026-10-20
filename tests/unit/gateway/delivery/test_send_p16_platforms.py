"""``indus send`` to the P16 E6–E10 and E12–E14 chat platforms through the real
``send_command.build_adapter``: the built-in registry row (its lazy factory and the package's own
``create_adapter``), the credentials read by the gateway's enablement pass (WhatsApp: enabled in
config), a send-only adapter that does no inbound work. Only the network is swapped: the package's
``create_adapter`` is wrapped to pass the wire fake's transport (and socket opener; WhatsApp: the
bridge launcher)."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable
from dataclasses import replace
from typing import Any

import pytest

from indus.foundation.config import loader
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


def test_indus_send_to_feishu(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import feishu as package
    from indus.gateway.adapters.feishu.adapter import FeishuAdapter
    from tests.fakes.platform.feishu_wire import FeishuWire

    clock: Any = scope.clock
    wire = FeishuWire(clock=clock)
    secrets: Any = scope.secrets
    secrets.set("FEISHU_APP_ID", wire.app_id)
    secrets.set("FEISHU_APP_SECRET", wire.app_secret)
    built = _wrap(monkeypatch, package, transport=wire.transport(), opener=wire.open)
    # The row's probe is httpx: sending needs no extra.
    adapter = send_command.build_adapter(
        scope, "feishu", _settings(scope, "feishu"), _registry("feishu")
    )
    assert isinstance(adapter, FeishuAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, "oc_group1", "deploy done").ok
    assert wire.platform.texts("oc_group1") == ["deploy done"]
    assert wire.socket_urls == [] and not wire.calls_to("POST", "/callback/ws/endpoint")


def test_indus_send_to_bluebubbles(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import bluebubbles as package
    from indus.gateway.adapters.bluebubbles.adapter import BlueBubblesAdapter
    from tests.fakes.platform.bluebubbles_wire import (
        MAX_LEN,
        PASSWORD,
        SERVER_URL,
        BlueBubblesWire,
    )

    clock: Any = scope.clock
    platform = FakeChatPlatform("bluebubbles", clock=clock, max_len=MAX_LEN)
    wire = BlueBubblesWire(platform)
    secrets: Any = scope.secrets
    secrets.set("BLUEBUBBLES_SERVER_URL", SERVER_URL)
    secrets.set("BLUEBUBBLES_PASSWORD", PASSWORD)
    built = _wrap(monkeypatch, package, transport=wire.transport())
    adapter = send_command.build_adapter(
        scope, "bluebubbles", _settings(scope, "bluebubbles"), _registry("bluebubbles")
    )
    assert isinstance(adapter, BlueBubblesAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, "iMessage;+;chat300", "deploy done").ok
    assert platform.texts("iMessage;+;chat300") == ["deploy done"]
    assert wire.webhooks == [] and not adapter.listener.accepting


def test_indus_send_to_simplex(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import simplex as package
    from indus.gateway.adapters.simplex.adapter import SimplexAdapter
    from tests.fakes.platform.simplex_wire import MAX_LEN, URL, SimplexWire

    clock: Any = scope.clock
    platform = FakeChatPlatform("simplex", clock=clock, max_len=MAX_LEN)
    wire = SimplexWire(platform)
    secrets: Any = scope.secrets
    secrets.set("SIMPLEX_WS_URL", URL)
    built = _wrap(monkeypatch, package, opener=wire.open)
    # The daemon socket is SimpleX's only transport, so a send needs the ``simplex`` extra too;
    # only the probe is forced here (the gate installs no extra).
    registry = _registry("simplex", socket_extra=True)
    adapter = send_command.build_adapter(scope, "simplex", _settings(scope, "simplex"), registry)
    assert isinstance(adapter, SimplexAdapter) and adapter is built[0] and adapter.send_only
    assert adapter.ws_url == URL
    assert _send(scope, adapter, "12", "deploy done").ok
    assert wire.commands[-1].startswith("/_send @12 json ") and wire.accepted == []


def test_indus_send_to_buzz(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import buzz as package
    from indus.gateway.adapters.buzz.adapter import BuzzAdapter
    from tests.contract.gateway.buzz_case import GROUP
    from tests.fakes.platform.buzz_wire import BOT_KEY, MAX_LEN, RELAY_URL, BuzzRelay

    clock: Any = scope.clock
    platform = FakeChatPlatform("buzz", clock=clock, max_len=MAX_LEN)
    platform.add_chat(GROUP, "group", "general")
    relay = BuzzRelay(platform)
    secrets: Any = scope.secrets
    secrets.set("BUZZ_RELAY_URL", RELAY_URL)
    secrets.set("BUZZ_PRIVATE_KEY", BOT_KEY)
    built = _wrap(monkeypatch, package, crypto=relay.crypto, opener=relay.open)
    # Signing (coincurve) and the relay socket (httpx-ws) are both the ``buzz`` extra; the fake
    # signer and relay stand in, so only the probe is forced.
    registry = _registry("buzz", socket_extra=True)
    adapter = send_command.build_adapter(scope, "buzz", _settings(scope, "buzz"), registry)
    assert isinstance(adapter, BuzzAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, GROUP, "deploy done").ok
    assert platform.texts(GROUP) == ["deploy done"]
    assert adapter.channels == {} and not [r for r in relay.requests if r.get("kinds") == [9]]


def test_indus_send_to_whatsapp(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import whatsapp as package
    from indus.gateway.adapters.whatsapp.adapter import WhatsAppAdapter
    from tests.fakes.platform.whatsapp_wire import FakeBridge, FakeLauncher

    # No credential: the bridge platform turns on in config (and is paired already).
    (scope.home / "config.yaml").write_text(
        "platforms:\n  whatsapp:\n    enabled: true\n", encoding="utf-8"
    )
    loader.reset()
    settings = load_gateway_settings(scope).platforms["whatsapp"]
    assert settings.enabled and settings.reason == "enabled in config"
    clock: Any = scope.clock
    bridge = FakeBridge()
    bridge.platform.clock = clock
    launcher = FakeLauncher(bridge)
    built = _wrap(monkeypatch, package, launcher=launcher, transport=bridge.transport)
    adapter = send_command.build_adapter(scope, "whatsapp", settings, _registry("whatsapp"))
    assert isinstance(adapter, WhatsAppAdapter) and adapter is built[0] and adapter.send_only
    chat = "15550000200@s.whatsapp.net"
    assert _send(scope, adapter, chat, "deploy done").ok
    sent = bridge.paths("/send")
    assert [(c.body["chat"], c.body["text"]) for c in sent] == [(chat, "deploy done")]
    # send-only: the running bridge is attached, never started, polled or stopped
    assert (launcher.attaches, launcher.ensures, launcher.stops) == (1, 0, 0)
    assert bridge.paths("/events") == []


def test_indus_send_to_weixin(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from indus.gateway.adapters import weixin as package
    from indus.gateway.adapters.weixin.adapter import WeixinAdapter, state_dir_for
    from tests.fakes.platform.weixin_wire import BOT_ACCOUNT, WeixinWire

    clock: Any = scope.clock
    wire = WeixinWire(clock=clock)
    secrets: Any = scope.secrets
    secrets.set("WEIXIN_TOKEN", wire.token)
    secrets.set("WEIXIN_ACCOUNT_ID", BOT_ACCOUNT)
    chat = "wxid_ana01"
    # The context token the gateway stored when Ana last wrote (no token: the send is refused).
    state = state_dir_for(scope.home, BOT_ACCOUNT)
    state.mkdir(parents=True)
    (state / "context_tokens.json").write_text(json.dumps({chat: wire.token_for(chat)}))
    built = _wrap(monkeypatch, package, transport=wire.transport())
    # Text needs only httpx; the row's probe (cryptography, the media AES) is base-installed.
    adapter = send_command.build_adapter(
        scope, "weixin", _settings(scope, "weixin"), _registry("weixin", socket_extra=True)
    )
    assert isinstance(adapter, WeixinAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, chat, "deploy done").ok
    assert wire.platform.texts(chat) == ["deploy done"]
    assert not [p for _, p, _ in wire.requests if p.endswith("/getupdates")]


def test_indus_send_to_yuanbao(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.gateway.adapters import yuanbao as package
    from indus.gateway.adapters.yuanbao.adapter import YuanbaoAdapter
    from tests.fakes.platform.yuanbao_wire import YuanbaoWire

    clock: Any = scope.clock
    platform = FakeChatPlatform("yuanbao", clock=clock)
    wire = YuanbaoWire(platform)
    secrets: Any = scope.secrets
    secrets.set("YUANBAO_APP_ID", wire.app_id)
    secrets.set("YUANBAO_APP_SECRET", wire.secret)
    built = _wrap(monkeypatch, package, transport=wire.transport(), opener=wire.open)
    # The bot socket is Yuanbao's only send path (its own send-only socket), so a real `indus
    # send` needs the ``yuanbao`` extra; the fake socket stands in and only the probe is forced.
    registry = _registry("yuanbao", socket_extra=True)
    adapter = send_command.build_adapter(scope, "yuanbao", _settings(scope, "yuanbao"), registry)
    assert isinstance(adapter, YuanbaoAdapter) and adapter is built[0] and adapter.send_only
    assert _send(scope, adapter, "ana-01", "deploy done").ok
    assert platform.texts("ana-01") == ["deploy done"]


def test_indus_send_refuses_raft(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("RAFT_PROFILE", "agent-one")
    settings = load_gateway_settings(scope)
    assert settings.platforms["raft"].enabled
    target = send_command._one_target("raft:anything")  # pyright: ignore[reportPrivateUsage]
    assert not isinstance(target, str)
    refused = send_command._platforms(target, settings)  # pyright: ignore[reportPrivateUsage]
    assert refused == "raft cannot receive messages"
    # nor is it offered when no platform is named
    every = send_command._one_target("all")  # pyright: ignore[reportPrivateUsage]
    if not isinstance(every, str):
        names = send_command._platforms(every, settings)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(names, str) or "raft" not in names
