"""Yuanbao tools (P16-22): bundle membership (R02.25), a scoped and approved DM (R02.26 spirit),
fenced and capped reading tools, stickers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.core.untrusted import is_untrusted
from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.yuanbao import create_adapter
from indus.gateway.adapters.yuanbao.adapter import YuanbaoAdapter, live_adapter
from indus.gateway.adapters.yuanbao.tools import (
    MAX_RESULT_CHARS,
    TOOL_NAMES,
    TOOLSET,
    YuanbaoTools,
    default_authorizer,
    register_tools,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.admission import GatewayIdentity, default_toolsets
from indus.gateway.core.authz import Authorizer
from indus.gateway.registry import PlatformRegistry
from indus.tools import toolsets
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry, scope_key
from tests.fakes.broker import FakeBroker, Reply
from tests.fakes.platform.yuanbao_wire import YuanbaoWire, group_member
from tests.fakes.terminal import terminal_scope
from tests.unit.gateway.adapters.yuanbao.test_adapter import Rig, drive

GROUP = "group:300"
MEMBERS = [
    group_member("u-ana", "Ana"),
    group_member("u-anabel", "Anabel", 1),
    group_member("u-bo", "Bo " + "x" * 200, 2),
]


@pytest.fixture
def rows() -> Iterator[None]:
    """The built-in toolset rows, read fresh (another test may have memoised a patched table)."""
    toolsets.clear_memo()
    yield
    toolsets.clear_memo()


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


def _authorizer(scope: Scope, adapter: YuanbaoAdapter) -> Authorizer:
    return default_authorizer(scope, adapter, PlatformRegistry())


class ToolRig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        chat: str = GROUP,
        platform: str = "yuanbao",
        allowed: str = "u-ana",
        broker_replies: list[Reply] | None = None,
    ) -> None:
        self.rig = Rig()
        self.rig.wire.groups["300"] = {
            "name": "Ops room",
            "owner_id": "u-bo",
            "owner_nickname": "Bo",
            "members": list(MEMBERS),
        }
        secrets = {"YUANBAO_APP_ID": "a", "YUANBAO_APP_SECRET": "b"}
        if allowed:
            secrets["INDUS_YUANBAO_ALLOWED_USERS"] = allowed
        self.scope = terminal_scope(tmp_path, secrets=secrets, clock=self.rig.clock)
        self.broker = FakeBroker(self.rig.clock, replies=list(broker_replies or []))
        self.identity = GatewayIdentity(platform=platform, chat_id=chat, user_id="u-ana")
        self.tools = YuanbaoTools(
            adapter_for=lambda _scope: self.rig.adapter if self.rig.adapter.connected else None,
            chat_for=lambda: self.identity,
            authorizer_for=_authorizer,
        )

    async def connect(self) -> None:
        await self.rig.connect()

    async def call(self, coro: Awaitable[str]) -> dict[str, Any]:
        text = await drive(self.rig.clock, coro)
        return _payload(text)

    def dm(self, **args: Any) -> Awaitable[str]:
        return self.tools.send_dm(args, scope=self.scope, session_id="s1", broker=self.broker)

    def c2c(self) -> list[dict[str, Any]]:
        return self.rig.sends("send_c2c_message")


def _payload(text: str) -> dict[str, Any]:
    """The JSON inside a fenced answer (or the answer itself)."""
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


# -- bundle membership (R02.25) --------------------------------------------------------------------


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_tools(registry)
    return registry


def test_the_tools_are_absent_from_every_other_bundle(rows: None) -> None:
    registry = _registry()
    names = set(TOOL_NAMES)
    for bundle in (
        "indus-cli",
        "indus-telegram",
        "indus-feishu",
        "indus-cron",
        "indus-acp",
        "indus-api-server",
        "indus-webhook",
        toolsets.CORE,
        "coding",
        "safe",
    ):
        assert not names & set(toolsets.resolve(bundle, registry)), bundle
    assert names <= set(toolsets.resolve("indus-yuanbao", registry))
    assert names <= set(toolsets.resolve("indus-gateway", registry))
    elsewhere = toolsets.select(None, registry=registry, platform="telegram").tools
    on_yuanbao = toolsets.select(None, registry=registry, platform="yuanbao").tools
    gateway_elsewhere = toolsets.select(["indus-gateway"], registry=registry, platform="feishu")
    assert not names & elsewhere and names <= on_yuanbao
    assert not names & gateway_elsewhere.tools
    assert default_toolsets("yuanbao") == ["indus-yuanbao"]
    for name in TOOL_NAMES:
        entry = registry.get(name)
        assert entry is not None and entry.toolset == TOOLSET
        assert entry.max_result_chars == MAX_RESULT_CHARS
        assert is_untrusted(name)


def test_the_probe_needs_the_bot_credentials(tmp_path: Path) -> None:
    tools = YuanbaoTools()
    with_keys = terminal_scope(tmp_path, secrets={"YUANBAO_APP_ID": "a", "YUANBAO_APP_SECRET": "b"})
    without = terminal_scope(tmp_path, name="other")
    assert tools.ready(with_keys) and not tools.ready(without)


# -- yuanbao_send_dm -------------------------------------------------------------------------------


def test_a_dm_to_a_user_outside_the_group_is_refused(tmp_path: Path) -> None:
    async def main() -> None:
        rig = ToolRig(tmp_path, allowed="*")
        await rig.connect()
        answer = await rig.call(rig.dm(user_id="u-zed", message="psst"))
        assert "not a member of this group" in answer["error"]
        assert rig.c2c() == [] and rig.broker.requests == []
        await rig.rig.close()

    run(main())


def test_a_dm_asks_for_approval_and_a_denial_sends_nothing(tmp_path: Path) -> None:
    async def main() -> None:
        rig = ToolRig(
            tmp_path, broker_replies=[Reply(choice="deny", reason="not now"), Reply(choice="once")]
        )
        await rig.connect()
        denied = await rig.call(rig.dm(user_id="u-ana", message="meeting at 3"))
        assert "error" in denied and rig.c2c() == []
        assert len(rig.broker.requests) == 1
        request = rig.broker.requests[0]
        assert request.kind == "approval"
        assert "u-ana" in json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
        sent = await rig.call(rig.dm(name="ana", message="meeting at 3"))
        # "ana" matches Ana and Anabel: the model must choose; nothing sent, nobody asked.
        assert "several members match" in sent["error"]
        assert {c["user_id"] for c in sent["candidates"]} == {"u-ana", "u-anabel"}
        assert len(rig.broker.requests) == 1
        done = await rig.call(rig.dm(user_id="u-ana", message="meeting at 3"))
        assert done["ok"] is True and done["user_id"] == "u-ana"
        assert len(rig.broker.requests) == 2
        (c2c,) = rig.c2c()
        assert c2c["to"] == "u-ana" and c2c["group_code"] == "300"
        assert c2c["elements"][0].get("text") == "meeting at 3"
        await rig.rig.close()

    run(main())


def test_a_recipient_the_gateway_would_not_accept_gets_nothing(tmp_path: Path) -> None:
    async def main() -> None:
        rig = ToolRig(tmp_path, allowed="u-bo", broker_replies=[Reply(choice="once")])
        await rig.connect()
        answer = await rig.call(rig.dm(user_id="u-ana", message="hi"))
        assert "may not receive direct messages" in answer["error"]
        assert rig.c2c() == [] and rig.broker.requests == []
        await rig.rig.close()

    run(main())


def test_dm_text_is_masked_bounded_and_needs_a_group(tmp_path: Path) -> None:
    async def main() -> None:
        rig = ToolRig(tmp_path, broker_replies=[Reply(choice="once")])
        await rig.connect()
        leaked = "".join(["gh", "p_", "Ab12" * 9])
        done = await rig.call(rig.dm(user_id="u-ana", message=f"the key is {leaked}"))
        assert done["ok"] is True
        text = rig.c2c()[0]["elements"][0].get("text")
        assert leaked not in text and text.startswith("the key is ")
        too_long = await rig.call(rig.dm(user_id="u-ana", message="x" * 10_001))
        assert "longer than" in too_long["error"]
        empty = await rig.call(rig.dm(user_id="u-ana", message="  "))
        assert "message is required" in empty["error"]
        rig.identity = GatewayIdentity(platform="yuanbao", chat_id="direct:u-ana")
        in_dm = await rig.call(rig.dm(user_id="u-ana", message="hi"))
        assert "group chat" in in_dm["error"]
        rig.identity = GatewayIdentity(platform="telegram", chat_id="-100")
        elsewhere = await rig.call(rig.dm(user_id="u-ana", message="hi"))
        assert "Yuanbao chat" in elsewhere["error"]
        assert len(rig.c2c()) == 1
        await rig.rig.close()

    run(main())


# -- reading tools ---------------------------------------------------------------------------------


def test_reading_tools_are_fenced_capped_and_for_the_current_group(
    tmp_path: Path, rows: None
) -> None:
    async def main() -> None:
        rig = ToolRig(tmp_path)
        await rig.connect()
        info_text = await drive(rig.rig.clock, rig.tools.group_info({}, scope=rig.scope))
        assert info_text.startswith("<indus-untrusted-data")
        info = _payload(info_text)
        assert info["name"] == "Ops room" and info["member_count"] == 3
        assert info["owner"] == {"user_id": "u-bo", "nickname": "Bo"}
        listing = _payload(
            await drive(rig.rig.clock, rig.tools.group_members({"limit": 2}, scope=rig.scope))
        )
        assert [m["user_id"] for m in listing["members"]] == ["u-ana", "u-anabel"]
        assert listing["next_offset"] == 2
        admins = _payload(
            await drive(
                rig.rig.clock, rig.tools.group_members({"action": "admins"}, scope=rig.scope)
            )
        )
        assert [m["role"] for m in admins["members"]] == ["admin", "owner"]
        assert all(len(m["nickname"]) <= 64 for m in admins["members"])
        found = _payload(
            await drive(
                rig.rig.clock,
                rig.tools.group_members({"action": "find", "name": "BO"}, scope=rig.scope),
            )
        )
        assert [m["user_id"] for m in found["members"]] == ["u-bo"]
        # Through dispatch: the core fences the answer once more and keeps it under the cap.
        registry = ToolRegistry()
        rig.tools.register(registry)
        result = await drive(
            rig.rig.clock,
            dispatch(
                ToolCall("yuanbao_query_group_members", {}, "c1"),
                ToolContext(scope=rig.scope),
                registry=registry,
            ),
        )
        assert "u-anabel" in result.text and len(result.text) <= MAX_RESULT_CHARS + 2_000
        rig.identity = GatewayIdentity(platform="yuanbao", chat_id="direct:u-ana")
        in_dm = _payload(await drive(rig.rig.clock, rig.tools.group_info({}, scope=rig.scope)))
        assert "group chat" in in_dm["error"]
        await rig.rig.close()

    run(main())


def test_tools_say_so_when_the_bot_is_not_connected(tmp_path: Path) -> None:
    async def main() -> None:
        rig = ToolRig(tmp_path)
        answer = _payload(await rig.tools.group_info({}, scope=rig.scope))
        assert "not connected" in answer["error"]
        sticker = _payload(await rig.tools.send_sticker({"sticker": "ok"}, scope=rig.scope))
        assert "not connected" in sticker["error"]
        no_scope = _payload(await rig.tools.group_info({}))
        assert "profile" in no_scope["error"]

    run(main())


# -- stickers --------------------------------------------------------------------------------------


def test_sticker_search_and_send_to_the_current_chat(tmp_path: Path) -> None:
    async def main() -> None:
        rig = ToolRig(tmp_path)
        await rig.connect()
        found = json.loads(await rig.tools.search_sticker({"query": "666"}))
        assert found["results"][0]["sticker_id"] == "278"
        thumbs = json.loads(await rig.tools.search_sticker({"query": "thumbs up", "limit": 3}))
        assert thumbs["results"][0]["name"] == "点赞"
        listing = json.loads(await rig.tools.search_sticker({"limit": 500}))
        assert listing["count"] == 50
        sent = _payload(
            await drive(rig.rig.clock, rig.tools.send_sticker({"sticker": "比心"}, scope=rig.scope))
        )
        assert sent["ok"] is True and sent["sticker"]["sticker_id"] == "252"
        last = rig.rig.wire.sends[-1]
        assert last["cmd"] == "send_group_message" and last["to"] == "300"
        assert json.loads(last["elements"][0].get("data"))["sticker_id"] == "252"
        unknown = _payload(
            await drive(rig.rig.clock, rig.tools.send_sticker({"sticker": "zzqx"}, scope=rig.scope))
        )
        assert "no sticker matches" in unknown["error"]
        random_one = _payload(
            await drive(rig.rig.clock, rig.tools.send_sticker({}, scope=rig.scope))
        )
        assert random_one["ok"] is True
        await rig.rig.close()

    run(main())


# -- the live adapter ------------------------------------------------------------------------------


def test_the_running_adapter_is_found_by_its_profile(scope: Scope) -> None:
    rig = Rig()
    wire: YuanbaoWire = rig.wire
    secrets: Any = scope.secrets
    secrets.set("YUANBAO_APP_ID", wire.app_id)
    secrets.set("YUANBAO_APP_SECRET", wire.secret)
    settings = PlatformSettings("yuanbao", True, "credentials", PlatformSection())
    adapter = create_adapter(
        AdapterConfig("yuanbao", settings, scope, rig.clock, None),
        transport=wire.transport(),
        opener=wire.open,
    )

    async def main() -> None:
        assert live_adapter(scope_key(scope)) is None
        await drive(rig.clock, adapter.connect())
        assert live_adapter(scope_key(scope)) is adapter
        await drive(rig.clock, adapter.disconnect())
        assert live_adapter(scope_key(scope)) is None

    run(main())
