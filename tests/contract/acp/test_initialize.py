"""P19-02: ``initialize`` and ``authenticate`` over a real JSON-RPC connection (fake client)."""

from __future__ import annotations

from pathlib import Path

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp.schema import InitializeResponse

from indus import __version__
from indus.acp.auth import SETUP_METHOD_ID
from indus.foundation.scope import Scope
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import open_harness
from tests.unit.backends._aio import aio

FAKE_KEY = "test" + "value" * 5


def keyed(scope: Scope) -> Scope:
    return scope.with_env(scope.env.with_overlay({"OPENROUTER_API_KEY": FAKE_KEY}))


INIT = {"protocolVersion": 1, "clientInfo": {"name": "zed", "version": "1.0"}}


@aio
async def test_capabilities_are_exact_and_valid(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        result = await h.call("initialize", INIT)
    InitializeResponse.model_validate(result)  # the vendor model accepts it
    assert result["protocolVersion"] == PROTOCOL_VERSION == 1
    assert result["agentInfo"] == {"name": "indus-agent", "version": __version__}
    assert result["agentCapabilities"] == {
        "loadSession": True,
        "promptCapabilities": {"image": True},
        "sessionCapabilities": {"fork": {}, "list": {}, "resume": {}},
    }


@aio
async def test_without_credentials_only_setup_is_offered_and_it_fails_until_a_key_exists(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        result = await h.call("initialize", INIT)
        assert result["authMethods"] == [
            {
                "type": "terminal",
                "id": SETUP_METHOD_ID,
                "name": "Set up Indus Agent",
                "description": result["authMethods"][0]["description"],
                "args": ["--setup"],
            }
        ]
        with pytest.raises(RequestError) as refused:
            await h.call("authenticate", {"methodId": SETUP_METHOD_ID})
        assert refused.value.code == -32000
        with pytest.raises(RequestError):
            await h.call("authenticate", {"methodId": "openrouter"})
        # setup ran out of band: a key now resolves
        h.agent.scope = keyed(scope)
        result = await h.call("initialize", INIT)
        ids = [m["id"] for m in result["authMethods"]]
        assert ids == ["openrouter", SETUP_METHOD_ID]
        assert "type" not in result["authMethods"][0]  # agent-managed method
        assert await h.call("authenticate", {"methodId": SETUP_METHOD_ID}) == {}
        assert await h.call("authenticate", {"methodId": " OpenRouter "}) == {}
        with pytest.raises(RequestError) as other:
            await h.call("authenticate", {"methodId": "anthropic"})
        assert other.value.code == -32000


@aio
async def test_form_elicitation_is_read_from_the_raw_params(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        await h.call("initialize", INIT)
        assert h.agent.form_elicitation is False
        await h.call(
            "initialize",
            {**INIT, "clientCapabilities": {"elicitation": {"form": {}}}},
        )
        assert h.agent.form_elicitation is True
        await h.call("initialize", {**INIT, "clientCapabilities": {"elicitation": {}}})
        assert h.agent.form_elicitation is False


@aio
async def test_unknown_methods_and_probes_answer_method_not_found(
    scope: Scope, store: StoreHandle, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        for method in ("ping", "health", "healthcheck", "no/such"):
            with pytest.raises(RequestError) as missing:
                await h.call(method, {})
            assert missing.value.code == -32601
    assert "Traceback" not in caplog.text
    assert not [r for r in caplog.records if r.exc_info]


@aio
async def test_bad_params_are_invalid_params(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        with pytest.raises(RequestError) as bad:
            await h.call("authenticate", {"methodId": 7})
        assert bad.value.code == -32602
