"""P15-20: platform deliverers in P14's registry, target resolution, truncation, mirror, R02.26."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cron import delivery as cron_delivery
from indus.cron import ports as cron_ports
from indus.cron.targets import DeliveryTarget
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import ChannelEntry
from indus.gateway.config import HomeChannel
from indus.gateway.delivery.directory import ChannelDirectory
from indus.gateway.delivery.mirror import Mirror
from indus.gateway.delivery.router import MAX_PLATFORM_OUTPUT, DeliveryRouter, cut_output
from indus.gateway.outbound.dead_targets import DeadTargets
from indus.gateway.outbound.sender import OutboundSender
from indus.state import gateway_routes as routes
from indus.state import registry as store_registry
from indus.state import session_meta
from indus.state.registry import StoreHandle
from tests.unit.gateway.outbound.recording import RecordingAdapter, fail, recording_adapter

HOMES = {"telegram": HomeChannel("telegram", "-100", thread_id="7"), "slack": None}


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = store_registry.open_unshared(scope)
    yield handle
    handle.close()


@pytest.fixture
def router(scope: Scope, store: StoreHandle) -> Iterator[DeliveryRouter]:
    directory = ChannelDirectory(scope)
    built = DeliveryRouter(
        scope, homes=lambda name: HOMES.get(name), directory=directory, mirror=Mirror(store)
    )
    yield built
    built.close()


def _attach(scope: Scope, router: DeliveryRouter, name: str, **kw: Any) -> RecordingAdapter:
    adapter = recording_adapter(name=name, markup="plain", **kw)
    sender = OutboundSender(adapter, scope=scope, platform=name, rng=lambda: 0.0)
    router.attach(name, sender)
    return adapter


def _job(deliver: str, **extra: Any) -> dict[str, Any]:
    return {"id": "a1b2c3d4e5f6", "name": "nightly report", "deliver": deliver, **extra}


def _deliver(scope: Scope, job: dict[str, Any], text: str, **kw: Any) -> Any:
    return asyncio.run(cron_delivery.deliver(scope, job, text, **kw))


def test_attached_platforms_register_with_p14_and_go_on_detach(
    scope: Scope, router: DeliveryRouter
) -> None:
    assert cron_delivery.deliverer_for("telegram") is None
    _attach(scope, router, "telegram")
    _attach(scope, router, "slack")
    assert cron_delivery.deliverer_for("telegram") is not None
    assert cron_delivery.deliverer_for("all") is not None
    assert {"telegram", "slack"} <= set(cron_delivery.registered_platforms())
    router.detach("telegram")  # a fatal disconnect
    assert cron_delivery.deliverer_for("telegram") is None
    assert cron_delivery.deliverer_for("all") is not None  # slack is still there
    router.detach("slack")
    assert cron_delivery.deliverer_for("all") is None


def test_reconnect_registers_again_without_leaving_a_stale_deliverer(
    scope: Scope, router: DeliveryRouter
) -> None:
    _attach(scope, router, "telegram")
    first = cron_delivery.deliverer_for("telegram")
    second_adapter = _attach(scope, router, "telegram")
    assert cron_delivery.deliverer_for("telegram") is not first
    result = _deliver(scope, _job("telegram:42"), "hello")
    assert result.status == "delivered" and second_adapter.ops[0].chat_id == "42"
    router.detach("telegram")
    assert cron_delivery.deliverer_for("telegram") is None


def test_home_channel_target_with_thread(scope: Scope, router: DeliveryRouter) -> None:
    adapter = _attach(scope, router, "telegram")
    result = _deliver(scope, _job("telegram"), "build is green")
    assert result.status == "delivered", result.reason
    (op,) = adapter.ops
    assert op.chat_id == "-100" and op.meta.get("thread_id") == "7"
    assert "build is green" in op.text and "nightly report" in op.text


def test_no_home_channel_is_a_named_failure(scope: Scope, router: DeliveryRouter) -> None:
    adapter = _attach(scope, router, "slack")
    result = _deliver(scope, _job("slack"), "hello")
    assert result.status == "delivery_failed" and "no_home_channel" in (result.reason or "")
    assert adapter.ops == []


def test_explicit_chat_and_thread(scope: Scope, router: DeliveryRouter) -> None:
    adapter = _attach(scope, router, "telegram")
    assert _deliver(scope, _job("telegram:-1001:17"), "x").status == "delivered"
    assert adapter.ops[0].chat_id == "-1001" and adapter.ops[0].meta["thread_id"] == "17"


def test_hash_name_resolves_through_the_directory(scope: Scope, router: DeliveryRouter) -> None:
    adapter = _attach(scope, router, "discord")

    async def channels() -> list[ChannelEntry]:
        return [
            ChannelEntry("111", "general", guild="Indus"),
            ChannelEntry("222", "ops", guild="Indus"),
        ]

    adapter.list_channels = channels  # type: ignore[method-assign]
    assert router.directory is not None
    asyncio.run(router.directory.build({"discord": adapter}))
    assert _deliver(scope, _job("discord:#general"), "hi").status == "delivered"
    assert adapter.ops[0].chat_id == "111"
    missing = _deliver(scope, _job("discord:#nowhere"), "hi")
    assert missing.status == "delivery_failed" and "unknown_channel" in (missing.reason or "")


def test_all_is_resolved_at_fire_time(scope: Scope, router: DeliveryRouter) -> None:
    tg = _attach(scope, router, "telegram")
    job = _job("all")
    assert _deliver(scope, job, "one").status == "delivered"
    assert len(tg.ops) == 1
    HOMES["discord"] = HomeChannel("discord", "555")
    try:
        dc = _attach(scope, router, "discord")  # connected after the job was made
        assert _deliver(scope, job, "two").status == "delivered"
        assert len(tg.ops) == 2 and [op.chat_id for op in dc.ops] == ["555"]
        # a platform without a home is reported, not silently skipped
        _attach(scope, router, "slack")
        partial = _deliver(scope, job, "three")
        assert partial.status == "delivery_failed" and "no_home_channel" in partial.reason
        assert len(tg.ops) == 3 and len(dc.ops) == 2
    finally:
        del HOMES["discord"]


def test_origin_resolves_to_the_jobs_chat(scope: Scope, router: DeliveryRouter) -> None:
    adapter = _attach(scope, router, "telegram")
    job = _job("origin", origin={"platform": "telegram", "chat_id": "900", "thread_id": "3"})
    assert _deliver(scope, job, "x").status == "delivered"
    assert adapter.ops[0].chat_id == "900" and adapter.ops[0].meta["thread_id"] == "3"


def test_not_connected_platform(scope: Scope, router: DeliveryRouter) -> None:
    receipt = asyncio.run(router.send(DeliveryTarget("telegram", "1"), "x"))
    assert not receipt.ok and "platform_not_connected" in (receipt.error or "")


def test_long_cron_output_is_cut_with_a_pointer(
    scope: Scope, router: DeliveryRouter, tmp_path: Path
) -> None:
    adapter = _attach(scope, router, "telegram", max_len=100_000)
    saved = tmp_path / "out.md"
    saved.write_text("x")
    receipt = asyncio.run(
        router.send(DeliveryTarget("telegram", "1"), "y" * 9000, output_path=saved)
    )
    assert receipt.ok
    (op,) = adapter.ops
    assert len(op.text) <= MAX_PLATFORM_OUTPUT and str(saved) in op.text
    # without a saved file there is nothing to point at: the text is chunked whole
    asyncio.run(router.send(DeliveryTarget("telegram", "1"), "z" * 9000))
    assert sum(len(o.text) for o in adapter.ops[1:]) >= 9000
    assert cut_output("short", saved) == "short"


def test_native_splitters_are_never_cut(scope: Scope, store: StoreHandle, tmp_path: Path) -> None:
    from dataclasses import replace

    adapter = recording_adapter(name="slack", max_len=100_000)
    type(adapter).capabilities = replace(type(adapter).capabilities, splits_natively=True)
    router = DeliveryRouter(scope, homes=lambda _n: None)
    try:
        router.attach("slack", OutboundSender(adapter, scope=scope, platform="slack"))
        saved = tmp_path / "out.md"
        asyncio.run(router.send(DeliveryTarget("slack", "C1"), "y" * 9000, output_path=saved))
        assert "".join(op.text for op in adapter.ops).count("y") == 9000
    finally:
        router.close()


def test_media_tags_in_cron_text_are_never_followed(scope: Scope, router: DeliveryRouter) -> None:
    image = scope.home / "cache" / "images" / "chart.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    adapter = _attach(scope, router, "telegram")
    text = f"see MEDIA:{image} and MEDIA:/etc/passwd"
    assert _deliver(scope, _job("telegram:1"), text).status == "delivered"
    assert [op.op for op in adapter.ops] == ["send"] and "MEDIA:/etc/passwd" in adapter.ops[0].text


def test_trusted_cron_attachments_are_sent(scope: Scope, router: DeliveryRouter) -> None:
    job = _job("telegram:1")
    folder = cron_ports.output_dir(scope, job["id"])
    folder.mkdir(parents=True, exist_ok=True)
    chart = folder / "chart.png"
    chart.write_bytes(b"png")
    adapter = _attach(scope, router, "telegram")
    result = _deliver(scope, job, "report", attachments=[chart])
    assert result.status == "delivered", result.reason
    assert [op.op for op in adapter.ops] == ["send", "image"]


def test_dead_chat_is_skipped(scope: Scope, router: DeliveryRouter) -> None:
    adapter = recording_adapter(name="telegram")
    dead = DeadTargets(None)
    router.attach("telegram", OutboundSender(adapter, scope=scope, dead_targets=dead))
    adapter.send_results.append(fail("forbidden", "Forbidden: bot was blocked by the user"))
    first = _deliver(scope, _job("telegram:9"), "x")
    assert first.status == "delivery_failed"
    second = _deliver(scope, _job("telegram:9"), "x")
    assert "chat_unreachable" in (second.reason or "") and len(adapter.ops) == 1


def test_delivered_cron_output_leaves_a_mirror_note(
    scope: Scope, router: DeliveryRouter, store: StoreHandle
) -> None:
    routes.get_or_create(
        store,
        "@default:telegram:dm:42",
        new_session_id=lambda: "s-42",
        platform="telegram",
        chat_type="dm",
        origin={"platform": "telegram", "chat_id": "42", "chat_type": "dm"},
    )
    _add_session(store, "s-42")
    _attach(scope, router, "telegram")
    assert _deliver(scope, _job("telegram:42"), "disk at 91%").status == "delivered"
    notes = session_meta.list_entries(store, "s-42", "gateway.mirror")
    (note,) = notes.values()
    assert "nightly report" in note["text"] and "disk at 91%" in note["text"]


def _add_session(store: StoreHandle, session_id: str) -> None:
    from tests.unit.state._helpers_d import add_session

    add_session(store, session_id)


def test_no_model_tool_wraps_the_router() -> None:
    """R02.26: delivery is a library; nothing the model can call reaches it."""
    import importlib
    import sys

    from indus.tools.builtin import BUILTIN_MODULES, load_builtins
    from indus.tools.registry import ToolRegistry

    registry = ToolRegistry()
    load_builtins(registry)
    names = registry.names()
    assert names, "the built-in tools should load"
    assert not [n for n in names if "send" in n.lower() or "deliver" in n.lower()]
    for module in BUILTIN_MODULES:
        loaded = sys.modules.get(module.path) or importlib.import_module(module.path)
        source = Path(loaded.__file__ or "").read_text(encoding="utf-8")
        assert "indus.gateway" not in source, module.path
