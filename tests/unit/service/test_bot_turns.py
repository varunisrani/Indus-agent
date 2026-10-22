"""Canonical bot chats run as bot turns (P24-07 follow-up, wave 45, D-16.980): platform ``bots``
and the profile's ``bots`` selection, so ``message_agent`` is offered there and nowhere else."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.core.persist import StateStore
from indus.core.turn_state import LoopOptions
from indus.foundation.scope import Scope
from indus.protocol.methods import SessionCreateParams
from indus.service.bot_turns import BOTS_PLATFORM, BotChats, bots_selection, is_canonical
from indus.service.session_service import SessionService
from indus.state import bot_chat, compression, messages, registry
from indus.state.registry import StoreHandle
from indus.tools import toolsets
from indus.tools.builtin import message_agent
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig, fake_options
from tests.fakes.model import FakeModelServer, fake_route, script_of
from tests.unit.backends._aio import aio

MESSAGE_AGENT = message_agent.TOOL_NAME


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def _registry() -> ToolRegistry:
    tools = ToolRegistry()
    message_agent.register_tools(tools)
    return tools


def _service(
    scope: Scope,
    store: StoreHandle,
    tmp_path: Path,
    *,
    seam: bool = True,
    loop: LoopOptions | None = None,
) -> SessionService:
    server = FakeModelServer(script_of(), clock=scope.clock)
    options = fake_options(server, session_id=None, registry=_registry(), loop=loop)
    return SessionService(
        scope,
        fake_route(server),
        toolsets=["indus-api-server"],
        options=options,
        store=StateStore(store),
        lock_dir=tmp_path / "locks",
        bot_chats=BotChats(scope, store).shape if seam else None,
    )


def test_the_selection_is_the_saved_bots_list_else_the_bundle(scope: Scope) -> None:
    assert bots_selection(scope) == ["indus-bots"]
    saved = scope.with_config(
        DictConfig({"platform_toolsets": {"bots": ["no_mcp", "bots", " web ", "bots", ""]}})
    )
    assert bots_selection(saved) == ["bots", "web"]
    empty = scope.with_config(DictConfig({"platform_toolsets": {"bots": ["no_mcp"]}}))
    assert bots_selection(empty) == ["indus-bots"]
    odd = scope.with_config(DictConfig({"platform_toolsets": {"bots": "file"}}))
    assert bots_selection(odd) == ["indus-bots"]


def test_the_canonical_chat_is_known_by_its_title_only(scope: Scope, store: StoreHandle) -> None:
    chat = bot_chat.ensure_canonical(store)
    assert is_canonical(store, chat.session_id)
    assert not is_canonical(store, "no-such-session")


@aio
async def test_the_bot_chat_offers_message_agent_and_no_other_session_does(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    chat = bot_chat.ensure_canonical(store)
    service = _service(scope, store, tmp_path, loop=LoopOptions(platform="api_server"))
    try:
        await service.resume(chat.session_id)
        bot = service.agent(chat.session_id)
        assert bot.options.platform == BOTS_PLATFORM
        assert bot.options.loop is not None and bot.options.loop.platform == BOTS_PLATFORM
        assert bot.toolset_selection == ["indus-bots"]
        assert MESSAGE_AGENT in bot.tool_names
        # Another session of the same service keeps the surface's selection and platform.
        other = (await service.create(SessionCreateParams())).session_id
        plain = service.agent(other)
        assert plain.options.platform != BOTS_PLATFORM
        assert plain.toolset_selection == ["indus-api-server"]
        assert MESSAGE_AGENT not in plain.tool_names
        # Even "every toolset" never brings the gated tool to a session off the bots platform.
        everything = (await service.create(SessionCreateParams(toolsets=None))).session_id
        assert MESSAGE_AGENT not in service.agent(everything).tool_names
    finally:
        await service.aclose()


@aio
async def test_the_profile_saved_bots_selection_is_respected(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    chat = bot_chat.ensure_canonical(store)
    narrowed = scope.with_config(DictConfig({"platform_toolsets": {"bots": ["web"]}}))
    service = _service(narrowed, store, tmp_path)
    service.bot_chats = BotChats(narrowed, store).shape
    try:
        await service.resume(chat.session_id)
        bot = service.agent(chat.session_id)
        assert bot.options.platform == BOTS_PLATFORM
        assert bot.toolset_selection == ["web"]
        assert MESSAGE_AGENT not in bot.tool_names  # the person left `bots` out
    finally:
        await service.aclose()


@aio
async def test_a_compressed_chat_tip_is_still_the_bot_chat(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    chat = bot_chat.ensure_canonical(store).session_id
    messages.append(store, chat, {"role": "user", "content": "hello"})
    assert compression.acquire(store, chat, "rotator")
    mark = compression.watermark(store, chat)
    summary = [{"role": "user", "content": "summary so far"}]
    tip = compression.publish_continuation(store, chat, {"id": "c1"}, summary, mark, "rotator")
    assert bot_chat.find_canonical(store) == tip and is_canonical(store, tip)
    service = _service(scope, store, tmp_path)
    try:
        await service.resume(tip)
        assert MESSAGE_AGENT in service.agent(tip).tool_names
    finally:
        await service.aclose()


@aio
async def test_without_the_seam_the_bot_chat_keeps_the_surface_selection(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    chat = bot_chat.ensure_canonical(store)
    service = _service(scope, store, tmp_path, seam=False)
    try:
        await service.resume(chat.session_id)
        agent = service.agent(chat.session_id)
        assert agent.toolset_selection == ["indus-api-server"]
        assert MESSAGE_AGENT not in agent.tool_names
    finally:
        await service.aclose()


def test_the_bots_gate_is_what_keeps_message_agent_in_bot_chats(scope: Scope) -> None:
    tools = _registry()
    on = toolsets.select(["indus-bots"], registry=tools, scope=scope, platform=BOTS_PLATFORM)
    off = toolsets.select(["indus-bots"], registry=tools, scope=scope, platform="api_server")
    assert MESSAGE_AGENT in on.tools and MESSAGE_AGENT not in off.tools
