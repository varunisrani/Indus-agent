"""Registry rows of the SDK sandbox backends and the shared ports (P21-12 wiring)."""

from __future__ import annotations

import asyncio
import importlib.metadata
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.backends import sdk_entries
from indus.backends.contract import BackendFlags, BackendUnavailable, Environment
from indus.backends.daytona import DAYTONA_FLAGS
from indus.backends.modal.exec import MODAL_FLAGS
from indus.backends.registry import (
    CLEANUP_BOUND_S,
    BackendEntry,
    CreateRequest,
    EnvironmentRegistry,
    EnvKey,
    default_entries,
)
from indus.backends.sdk_entries import (
    SDK_CLEANUP_BOUND_S,
    extra_missing,
    modal_probe,
)
from indus.backends.sdk_entries import (
    sdk_entries as all_sdk_entries,
)
from indus.backends.sdk_ports import ProfileRecords, VendorSupport
from indus.backends.vercel.exec import VERCEL_FLAGS
from tests.fakes.backend.environment import FakeEnvironment
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import terminal_scope
from tests.unit.backends._aio import aio
from tests.unit.backends.sdk_rig import record_of, seed_record

SDK_MODULES = ("modal", "daytona", "vercel", "vercel.sandbox", "vercel.sandbox.sync")
SDK_ROWS = ("modal", "daytona", "vercel_sandbox")


def _installed(_: str) -> object:
    return object()


def _absent(name: str) -> object:
    raise importlib.metadata.PackageNotFoundError(name)


def test_the_registry_lists_seven_built_in_backends() -> None:
    names = [entry.name for entry in default_entries()]
    assert names == [
        "local",
        "docker",
        "ssh",
        "singularity",
        "modal",
        "daytona",
        "vercel_sandbox",
    ]


def test_row_flags_match_the_environments() -> None:
    rows = {entry.name: entry.flags for entry in all_sdk_entries()}
    assert rows == {"modal": MODAL_FLAGS, "daytona": DAYTONA_FLAGS, "vercel_sandbox": VERCEL_FLAGS}
    assert all(isinstance(flags, BackendFlags) and flags.isolated for flags in rows.values())


def test_sdk_rows_get_a_cleanup_bound_for_sync_back_and_snapshot() -> None:
    assert SDK_CLEANUP_BOUND_S == 120 + 60 + 15
    registry = EnvironmentRegistry()
    for name in registry.names():
        expected = SDK_CLEANUP_BOUND_S if name in SDK_ROWS else CLEANUP_BOUND_S
        assert registry.cleanup_bound_for(EnvKey("/p", "session", "s", name)) == expected


def test_a_rows_own_bound_is_what_its_cleanup_gets(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))

    class Slow(FakeEnvironment):
        async def cleanup(self) -> None:
            await clock.sleep(100)
            await super().cleanup()

    made: list[Slow] = []

    async def factory(request: CreateRequest) -> Environment:
        made.append(Slow(cwd="/w"))
        return made[-1]

    async def go() -> None:
        registry = EnvironmentRegistry(
            [BackendEntry("slow", factory, BackendFlags(), cleanup_bound_s=150.0)], clock=clock
        )
        scope = terminal_scope(tmp_path, {"terminal": {"backend": "slow"}})
        await registry.acquire(scope, session_id="s")
        closing = asyncio.ensure_future(registry.shutdown(fence=False))
        for _ in range(400):
            await asyncio.wait({closing}, timeout=0.002)
            if closing.done():
                break
            clock.advance(1)
        assert closing.done()

    asyncio.run(go())
    assert made[0].cleaned == 1  # 100 s is past the default 15 s bound, inside the row's 150 s


def test_a_missing_extra_names_it_without_importing_the_sdk(tmp_path: Path) -> None:
    before = {name for name in SDK_MODULES if name in sys.modules}
    for backend, extra in (
        ("modal", "modal"),
        ("daytona", "daytona"),
        ("vercel_sandbox", "vercel"),
    ):
        reason = extra_missing(backend, lookup=_absent) or ""
        assert f"`{extra}` extra" in reason and backend in reason
        assert extra_missing(backend, lookup=_installed) is None
    scope = terminal_scope(tmp_path, {"terminal": {"backend": "modal"}}, secrets={})
    sdk_entries.modal_probe(scope)
    sdk_entries.daytona_probe(scope)
    sdk_entries.vercel_probe(scope)
    assert {name for name in SDK_MODULES if name in sys.modules} == before


def test_probes_report_credentials_once_the_extra_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importlib.metadata, "distribution", _installed)
    bare = terminal_scope(tmp_path, {"terminal": {}}, secrets={})
    assert "MODAL_TOKEN_ID" in (sdk_entries.modal_probe(bare) or "")
    assert "DAYTONA_API_KEY" in (sdk_entries.daytona_probe(bare) or "")
    assert "VERCEL_TOKEN" in (sdk_entries.vercel_probe(bare) or "")
    ready = terminal_scope(
        tmp_path,
        {"terminal": {}},
        secrets={"MODAL_TOKEN_ID": "i" + "1", "MODAL_TOKEN_SECRET": "s" + "1"},
    )
    assert modal_probe(ready) is None
    managed = terminal_scope(tmp_path, {"terminal": {"modal_mode": "managed"}}, secrets={})
    assert "D-16.16" in (modal_probe(managed) or "")


@aio
async def test_the_registry_refuses_a_backend_whose_probe_fails(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path, {"terminal": {"backend": "daytona"}}, secrets={})
    registry = EnvironmentRegistry([sdk_entries.daytona_entry()])
    try:
        with pytest.raises(BackendUnavailable):
            await registry.acquire(scope, session_id="s")
    finally:
        await registry.shutdown(fence=False)


@pytest.mark.parametrize(
    ("backend", "opener", "kind"),
    [
        ("modal", "indus.backends.modal.exec.open_modal", "modal"),
        ("daytona", "indus.backends.daytona.open_daytona", None),
        ("vercel_sandbox", "indus.backends.vercel.exec.open_vercel", "vercel"),
    ],
)
def test_factories_hand_the_shared_ports_and_the_task_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    opener: str,
    kind: str | None,
) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_open(scope: Any, **kwargs: Any) -> Environment:
        seen.append(kwargs)
        return FakeEnvironment(cwd="/w")

    module, name = opener.rsplit(".", 1)
    monkeypatch.setattr(sys.modules[module], name, fake_open)
    entry = {e.name: e for e in all_sdk_entries()}[backend]
    entry = BackendEntry(
        entry.name,
        entry.factory,
        entry.flags,
        probe=None,
        profile_scoped=entry.profile_scoped,
        cleanup_bound_s=entry.cleanup_bound_s,
    )

    async def go() -> None:
        registry = EnvironmentRegistry([entry])
        try:
            for persistent in (True, False):
                scope = terminal_scope(
                    tmp_path,
                    {"terminal": {"backend": backend, "container_persistent": persistent}},
                    secrets={},
                )
                await registry.acquire(scope, session_id="sess-1")
        finally:
            await registry.shutdown(fence=False)

    asyncio.run(go())
    assert [kw["task_key"] for kw in seen] == ["shared", "sess-1"]
    for kw in seen:
        assert isinstance(kw["support"], VendorSupport) and kw["session_id"] == "sess-1"
        if kind is None:
            assert "records" not in kw
        else:
            records = kw["records"]
            assert isinstance(records, ProfileRecords) and records.store.kind == kind


@aio
async def test_profile_records_run_off_the_loop_and_drop_only_the_named_snapshot(
    tmp_path: Path,
) -> None:
    scope = terminal_scope(tmp_path, {})
    records = ProfileRecords.for_scope(scope, "modal")
    assert await records.load("t1") is None
    await records.save("t1", {"snapshot_id": "im-1"})
    loaded = await records.load("t1")
    assert loaded is not None and loaded["snapshot_id"] == "im-1"
    await records.drop("t1", expect={"snapshot_id": "im-other"})
    assert record_of(scope, "modal", "t1") == {"snapshot_id": "im-1"}
    await records.drop("t1", expect={"snapshot_id": "im-1"})
    assert record_of(scope, "modal", "t1") is None
    seed_record(scope, "modal", "t2", snapshot_id="im-2")
    await records.drop("t2")
    assert record_of(scope, "modal", "t2") is None
    with pytest.raises(ValueError, match="no field"):
        await records.save("t1", {"image": "x"})


@aio
async def test_vendor_support_bounds_a_call_and_chunks_stdin() -> None:
    clock = FakeClock(datetime(2026, 10, 19, tzinfo=UTC))
    support = VendorSupport(clock=clock)
    assert await support.call("get", lambda: 7, timeout=5) == 7
    assert support.guard("echo hi", 12).startswith("timeout -k 2 12 bash -c ")
    chunks = support.stdin_chunks("x" * (64 * 1024 + 3))
    assert [len(c) for c in chunks] == [65536, 3]
    with pytest.raises(ValueError):
        support.stdin_chunks("bad \ud800")
