"""P19-03: host selection (D-16.83) and the in-process host's own rules."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from acp import RequestError

from indus.acp.agent import AcpAgent
from indus.acp.backend import (
    DAEMON_UNAVAILABLE_TEXT,
    AcpBackend,
    BackendError,
    CreateRequest,
    HostSelector,
)
from indus.acp.local_host import LocalHost
from indus.foundation.scope import Scope
from indus.state import messages, registry, sessions
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import local_backend
from tests.fakes.acp_client import AcpPair
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder
from tests.unit.backends._aio import aio


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


class Counted(HostSelector):
    calls: list[str]


def _selector(local: LocalHost | None, *, local_only: bool, fallback: bool) -> Counted:
    calls: list[str] = []

    async def attached() -> AcpBackend:
        calls.append("attached")
        raise BackendError("daemon_unavailable", "no daemon")

    async def in_process() -> AcpBackend:
        calls.append("local")
        assert local is not None
        return local

    selector = Counted(
        local_only=local_only, allow_fallback=fallback, attached=attached, local=in_process
    )
    selector.calls = calls
    return selector


@aio
async def test_no_daemon_and_no_fallback_answers_initialize_then_refuses_sessions(
    scope: Scope, tmp_path: Path
) -> None:
    selector = _selector(None, local_only=False, fallback=False)
    pair = AcpPair(AcpAgent(scope, selector, env={}))
    try:
        assert (await pair.call("initialize", {"protocolVersion": 1}))["protocolVersion"] == 1
        with pytest.raises(RequestError) as refused:
            await pair.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        assert str(refused.value) == DAEMON_UNAVAILABLE_TEXT
        assert "indus acp --local" in DAEMON_UNAVAILABLE_TEXT
        assert (refused.value.data or {}).get("reason") == "daemon_unavailable"
        assert selector.calls == ["attached"]
    finally:
        await pair.close()


@pytest.mark.parametrize(("local_only", "fallback"), [(False, True), (True, False)])
@aio
async def test_fallback_or_local_flag_runs_sessions_here(
    scope: Scope, store: StoreHandle, tmp_path: Path, local_only: bool, fallback: bool
) -> None:
    local, _server = local_backend(scope, store, tmp_path)
    selector = _selector(local, local_only=local_only, fallback=fallback)
    pair = AcpPair(AcpAgent(scope, selector, env={}))
    try:
        result = await pair.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        assert result["sessionId"]
        assert selector.built is local and local.mode == "local"
        expected = ["local"] if local_only else ["attached", "local"]
        assert selector.calls == expected
    finally:
        await pair.close()


@aio
async def test_a_retried_input_id_returns_the_first_admission(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    local, _server = local_backend(scope, store, tmp_path, text_turn("one"), text_turn("two"))
    try:
        snap = await local.create(CreateRequest(cwd=str(tmp_path), request_id="r1"))
        first = await local.submit(snap.session_id, "hello", input_id="i1")
        again = await local.submit(snap.session_id, "hello", input_id="i1")
        assert again.admission_id == first.admission_id
        await eventually(
            lambda: any(m["role"] == "assistant" for m in messages.get(store, snap.session_id))
        )
        users = [m for m in messages.get(store, snap.session_id) if m["role"] == "user"]
        assert len(users) == 1
        await eventually(lambda: local._turns[first.admission_id].admission.state == "terminal")  # pyright: ignore[reportPrivateUsage]
        receipt = await local.receipt(snap.session_id, first.admission_id)
        assert (receipt.state, receipt.outcome) == ("terminal", "completed")
        assert await local.cancel(snap.session_id, first.admission_id) == "settled"
        assert await local.cancel(snap.session_id, "nope") == "not_found"
    finally:
        await local.aclose()


@aio
async def test_a_workspace_move_rebuilds_the_agent_in_the_new_folder(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    sessions.create(store, "s-w", "acp", cwd=str(old))
    messages.append(store, "s-w", {"role": "user", "content": "hi"})
    local, _server = local_backend(scope, store, tmp_path)
    try:
        snap = await local.resume("s-w")
        service = await local.service()
        assert service.agent(snap.session_id).options.cwd == Path(old)
        result = await local.mutate("s-w", "workspace", {"cwd": str(new)})
        assert result.status == "applied"
        assert service.agent("s-w").options.cwd == Path(new)
        row = sessions.get(store, "s-w")
        assert row is not None and row["cwd"] == str(new)
        with pytest.raises(BackendError) as unavailable:
            await local.mutate("s-w", "model", {"model": "x"})
        assert unavailable.value.code == "mutation_unavailable"
    finally:
        await local.aclose()


@aio
async def test_a_busy_session_is_not_moved(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    local, _server = local_backend(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("done"),
        tools={"hold": holder},
    )
    try:
        service = await local.service()
        snap = await local.create(CreateRequest(cwd=str(tmp_path), request_id="r2"))
        await local.submit(snap.session_id, "go", input_id="i2")
        await eventually(lambda: holder.entered == 1)
        assert not service.is_idle(snap.session_id)
        with pytest.raises(BackendError) as busy:
            await local.mutate(snap.session_id, "workspace", {"cwd": str(tmp_path / "x")})
        assert busy.value.code == "session_busy"
        holder.gate.set()
        await eventually(lambda: service.is_idle(snap.session_id))
    finally:
        await local.aclose()
