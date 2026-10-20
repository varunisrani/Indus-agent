"""P19-12: the model picker (config option + ``/model``) and the single ask-before-edits mode."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError

from indus.acp.local_host import LocalHost
from indus.acp.model_mode import parse_model_id
from indus.foundation.scope import Scope
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import KINDS, Harness, open_harness
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder
from tests.unit.backends._aio import aio


async def _new(h: Harness, tmp_path: Path) -> str:
    return str((await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}))["sessionId"])


async def _local(h: Harness, guard: str | None = None) -> LocalHost:
    host = await h.backend()
    assert isinstance(host, LocalHost)
    service = await host.service()

    def resolve(model: str, provider: str | None) -> Any:
        if model == "broken":
            raise ValueError("no such model at this provider")
        return replace(service.route, model=model)

    host.resolve_route = resolve
    host.model_guard = lambda _used, _window: guard
    return host


def _choose(option_id: str) -> Any:
    def answer(_params: Mapping[str, Any]) -> dict[str, Any]:
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    return answer


def _model_of(host: LocalHost, sid: str) -> str:
    service = host._service  # pyright: ignore[reportPrivateUsage]
    assert service is not None
    return service.agent(sid).binding.state.model


def test_model_ids() -> None:
    assert parse_model_id("openrouter:anthropic/claude") == ("openrouter", "anthropic/claude")
    assert parse_model_id("custom:lab:llama-3") == ("custom:lab", "llama-3")
    assert parse_model_id("gpt-x") == (None, "gpt-x")


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_only_the_ask_mode_exists(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness(kind, scope, store, tmp_path) as h:
        sid = await _new(h, tmp_path)
        assert await h.call("session/set_mode", {"sessionId": sid, "modeId": "default"}) == {}
        with pytest.raises(RequestError) as refused:
            await h.call("session/set_mode", {"sessionId": sid, "modeId": "accept-edits"})
        assert (refused.value.data or {}).get("reason") == "acp_edit_policy_mutation_unavailable"
        with pytest.raises(RequestError) as policy:
            await h.call(
                "session/set_config_option",
                {"sessionId": sid, "configId": "edit_approval_policy", "value": "auto"},
            )
        assert (policy.value.data or {}).get("reason") == "acp_edit_policy_mutation_unavailable"
        same = await h.call(
            "session/set_config_option",
            {"sessionId": sid, "configId": "edit_approval_policy", "value": "ask"},
        )
        other = await h.call(
            "session/set_config_option", {"sessionId": sid, "configId": "theme", "value": "dark"}
        )
        assert same == other  # accepted, nothing changed
        assert [o["id"] for o in other["configOptions"]] in (["model"], [])


@aio
async def test_a_model_pick_switches_the_session_only(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        sid = await _new(h, tmp_path)
        host = await _local(h)
        answer = await h.call(
            "session/set_config_option",
            {"sessionId": sid, "configId": "model", "value": "fake:faster"},
        )
        assert _model_of(host, sid) == "faster"
        assert answer["configOptions"][0]["currentValue"] == "fake:faster"
        assert scope.config is None or scope.config.get("model") is None  # profile untouched


@aio
async def test_a_declined_guarded_pick_changes_nothing(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    script = {"session/request_permission": _choose("cancel")}
    async with open_harness("local", scope, store, tmp_path, script=script) as h:
        sid = await _new(h, tmp_path)
        host = await _local(h, guard="the conversation does not fit")
        before = _model_of(host, sid)
        with pytest.raises(RequestError) as cancelled:
            await h.call(
                "session/set_config_option",
                {"sessionId": sid, "configId": "model", "value": "fake:tiny"},
            )
        assert (cancelled.value.data or {}).get("reason") == "model_switch_cancelled"
        assert _model_of(host, sid) == before
        [card] = [r["params"] for r in h.pair.requests]
        assert [(o["optionId"], o["kind"]) for o in card["options"]] == [
            ("allow_once", "allow_once"),
            ("cancel", "reject_once"),
        ]
        assert "does not fit" in card["toolCall"]["content"][0]["content"]["text"]


@aio
async def test_an_accepted_guarded_pick_switches(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    script = {"session/request_permission": _choose("allow_once")}
    async with open_harness("local", scope, store, tmp_path, script=script) as h:
        sid = await _new(h, tmp_path)
        host = await _local(h, guard="the conversation does not fit")
        await h.call(
            "session/set_config_option", {"sessionId": sid, "configId": "model", "value": "tiny"}
        )
        assert _model_of(host, sid) == "tiny"


@aio
async def test_a_pick_during_a_turn_waits_for_the_next_one(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    turns = (tool_batch(("hold", {})), text_turn("done"), tool_batch(("hold", {})), text_turn("x"))
    async with open_harness("local", scope, store, tmp_path, *turns, tools={"hold": holder}) as h:
        sid = await _new(h, tmp_path)
        host = await _local(h)
        before = _model_of(host, sid)
        running = asyncio.ensure_future(
            h.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
        )
        await eventually(lambda: holder.entered == 1, limit=10)
        await h.call(
            "session/set_config_option", {"sessionId": sid, "configId": "model", "value": "later"}
        )
        texts = [
            u["content"]["text"]
            for u in h.pair.updates(sid)
            if "content" in u and isinstance(u["content"], dict)
        ]
        assert "later applies from your next message.\n" in texts
        assert _model_of(host, sid) == before
        holder.gate.set()
        await asyncio.wait_for(running, 10)
        await eventually(lambda: _model_of(host, sid) == "later", limit=5)


@aio
async def test_a_deferred_pick_refused_at_the_boundary_is_announced(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    turns = (tool_batch(("hold", {})), text_turn("done"))
    async with open_harness("local", scope, store, tmp_path, *turns, tools={"hold": holder}) as h:
        sid = await _new(h, tmp_path)
        await _local(h)
        running = asyncio.ensure_future(
            h.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
        )
        await eventually(lambda: holder.entered == 1, limit=10)
        reply = await h.call(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": "/model broken"}]},
        )
        assert reply["stopReason"] == "end_turn"
        holder.gate.set()
        await asyncio.wait_for(running, 10)

        def announced() -> bool:
            return any(
                "The model switch to broken was not applied" in u["content"]["text"]
                for u in h.pair.updates(sid)
                if u["sessionUpdate"] == "agent_message_chunk"
            )

        await eventually(announced, limit=5)


@aio
async def test_slash_model_without_an_argument_names_the_model(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        sid = await _new(h, tmp_path)
        await h.call(
            "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "/model"}]}
        )
        texts = "".join(
            u["content"]["text"]
            for u in h.pair.updates(sid)
            if u["sessionUpdate"] == "agent_message_chunk"
        )
        assert "Current model:" in texts and "Provider: fake" in texts
