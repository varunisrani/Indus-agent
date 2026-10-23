"""W50-a: the daemon's session switches (``service.daemon_config``), its server commands for them
(``DaemonSlashHost``), the host's ``/yolo`` and the queued count ``/status`` shows."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.core import fast_mode
from indus.foundation.scope import Scope
from indus.protocol.bridge import ConfigKey, ConfigSetParams
from indus.protocol.errors import INVALID_PARAMS
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.safety.approvals.decision import APPROVALS, session_key
from indus.service import daemon_config
from indus.service.daemon_host import DaemonSessionHost
from indus.service.daemon_slash import DaemonSlashHost
from indus.state import registry
from indus.state.registry import StoreHandle
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import Holder, open_host
from tests.unit.backends._aio import aio


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


async def _session(host: DaemonSessionHost) -> tuple[str, str]:
    created = await host.create(SessionCreateParams(), source="tui")
    key = host.key_of(created.session_id)
    assert key is not None
    return key, created.session_id


def _set(sid: str, key: ConfigKey, value: Any) -> ConfigSetParams:
    return ConfigSetParams(key=key, value=value, session_id=sid)


def _infos(host: DaemonSessionHost, key: str) -> int:
    found = host.event_log.since(key, host.replay_epoch, 0)
    events = getattr(found, "events", ())
    return sum(1 for e in events if e.get("type") == "session.info")


def _yolo(scope: Scope, sid: str) -> bool:
    return APPROVALS.auto_approve(session_key(scope, sid))


@aio
async def test_each_switch_applies_to_the_shared_session_and_announces(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    key, sid = await _session(host)
    before = _infos(host, key)
    yolo = await daemon_config.set_switch(host, key, _set(sid, ConfigKey.YOLO, True), scope=scope)
    fast = await daemon_config.set_switch(host, key, _set(sid, ConfigKey.FAST, "fast"), scope=scope)
    effort = await daemon_config.set_switch(
        host, key, _set(sid, ConfigKey.REASONING, "high"), scope=scope
    )
    await eventually(lambda: _infos(host, key) >= before + 3)
    assert (yolo.value, yolo.applied.value, yolo.saved) == (True, "now", False)
    assert _yolo(scope, sid)
    assert fast.value == "fast"
    assert fast_mode.FAST_MODES.get(fast_mode.session_key(scope, sid)) == "fast"
    assert effort.value == (host.service.agent(sid).binding.state.reasoning_effort or "none")
    await daemon_config.set_switch(host, key, _set(sid, ConfigKey.YOLO, False), scope=scope)
    assert not _yolo(scope, sid)
    await host.aclose()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (ConfigKey.YOLO, "on", "value must be true or false"),
        (ConfigKey.FAST, "warp", "value must be one of"),
        (ConfigKey.REASONING, " ", "value must be a reasoning level"),
        (ConfigKey.MODEL, "", "value must be a model name"),
    ],
)
@aio
async def test_bad_values_are_refused_before_anything_changes(
    scope: Scope, store: StoreHandle, tmp_path: Path, name: ConfigKey, value: Any, message: str
) -> None:
    host = await open_host(scope, store, tmp_path)
    key, sid = await _session(host)
    with pytest.raises(ProtocolError) as refused:
        await daemon_config.set_switch(host, key, _set(sid, name, value), scope=scope)
    assert refused.value.error == INVALID_PARAMS and refused.value.path == "value"
    assert refused.value.message.startswith(message)
    assert not _yolo(scope, sid)
    await host.aclose()


@aio
async def test_slash_switches_run_through_the_daemons_config_set(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    host = await open_host(scope, store, tmp_path)
    key, sid = await _session(host)
    slash = DaemonSlashHost(host, key, scope=scope)
    assert "yolo" not in slash.refused and "fast" not in slash.refused
    assert await slash.set_switch(sid, ConfigKey.YOLO, True) is True
    assert _yolo(scope, sid)
    assert await slash.set_switch(sid, ConfigKey.FAST, "fast") == "fast"
    assert await slash.set_switch(sid, ConfigKey.YOLO, False) is False
    await host.aclose()


@aio
async def test_queued_inputs_count_only_what_waits(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(
        scope,
        store,
        tmp_path,
        tool_batch(("hold", {})),
        text_turn("done"),
        text_turn("two"),
        tools={"hold": holder},
    )
    key, _sid = await _session(host)
    slash = DaemonSlashHost(host, key, scope=scope)
    assert await slash.queued(key) == 0
    await host.submit(key, "work", principal="p", request_id="r1")
    await eventually(lambda: holder.entered)
    assert await slash.queued(key) == 0  # the running input is not waiting
    await host.submit(key, "two", principal="p", request_id="r2", busy="queue")
    assert await host.queued_inputs(key) == 1 and await slash.queued(key) == 1
    holder.gate.set()
    await eventually(lambda: host.running_generation(key) is None)
    await host.aclose()


@aio
async def test_a_persons_yolo_is_not_undone_by_an_unattended_turns_end(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    host = await open_host(
        scope, store, tmp_path, tool_batch(("hold", {})), text_turn("done"), tools={"hold": holder}
    )
    key, sid = await _session(host)
    receipt = await host.submit(key, "work", principal="p", request_id="r1", unattended=True)
    await eventually(lambda: holder.entered)
    assert _yolo(scope, sid)  # the unattended turn's own switch
    await host.set_auto_approve(key, True)
    holder.gate.set()
    assert (await host.wait(receipt.admission_id)).outcome == "completed"
    assert _yolo(scope, sid)
    await host.set_auto_approve(key, False)
    assert not _yolo(scope, sid)
    await host.aclose()
