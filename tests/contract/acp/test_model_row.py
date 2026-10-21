"""P19 follow-up: a model picked in the editor is written to the session row, and a stored session
comes back on it (``--local``; the daemon side is in ``tests/unit/service/test_acp_shape.py``)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from indus.acp.local_host import LocalHost
from indus.foundation.scope import Scope
from indus.state import sessions
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import Harness, open_harness, seed_session
from tests.unit.backends._aio import aio

LOAD = {"mcpServers": []}


async def _local(h: Harness) -> LocalHost:
    host = await h.backend()
    assert isinstance(host, LocalHost)
    service = await host.service()

    def resolve(model: str, provider: str | None) -> Any:
        return replace(service.route, model=model, provider=provider or service.route.provider)

    host.resolve_route = resolve
    return host


def _model_of(host: LocalHost, sid: str) -> str:
    service = host._service  # pyright: ignore[reportPrivateUsage]
    assert service is not None
    return service.agent(sid).binding.state.model


@aio
async def test_a_pick_is_written_to_the_row_and_a_new_editor_process_resumes_on_it(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    seed_session(store, "s-stored", cwd=str(tmp_path), rows=[{"role": "user", "content": "hi"}])
    async with open_harness("local", scope, store, tmp_path) as h:
        host = await _local(h)
        await h.call("session/load", {"sessionId": "s-stored", "cwd": str(tmp_path), **LOAD})
        await h.call(
            "session/set_config_option",
            {"sessionId": "s-stored", "configId": "model", "value": "fake:faster"},
        )
        assert _model_of(host, "s-stored") == "faster"
    row = sessions.get(store, "s-stored")
    assert row is not None and row["model"] == "faster" and row["billing_provider"] == "fake"

    async with open_harness("local", scope, store, tmp_path) as again:  # the editor restarted
        host = await _local(again)
        loaded = await again.call(
            "session/load", {"sessionId": "s-stored", "cwd": str(tmp_path), **LOAD}
        )
        assert _model_of(host, "s-stored") == "faster"
        assert loaded["configOptions"][0]["currentValue"] == "fake:faster"


@aio
async def test_a_new_session_is_built_on_the_profile_model(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        host = await _local(h)
        made = await h.call("session/new", {"cwd": str(tmp_path), **LOAD})
        service = await host.service()
        assert _model_of(host, made["sessionId"]) == service.route.model


@aio
async def test_a_stored_model_that_cannot_be_resolved_keeps_the_profile_model(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    seed_session(store, "s-old", cwd=str(tmp_path), rows=[{"role": "user", "content": "hi"}])
    store.write(
        lambda conn: conn.execute("UPDATE sessions SET model = 'retired-model' WHERE id = 's-old'")
    )
    async with open_harness("local", scope, store, tmp_path) as h:
        host = await _local(h)

        def refuse(model: str, provider: str | None) -> Any:
            raise ValueError(f"{model} is gone")

        host.resolve_route = refuse
        await h.call("session/load", {"sessionId": "s-old", "cwd": str(tmp_path), **LOAD})
        service = await host.service()
        assert _model_of(host, "s-old") == service.route.model
