"""P15-29: gateway hook folders — discovery, dispatch, isolation, deadlines, and the daemon
bridge (agent start/end, command hooks) on a real pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import textwrap
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.daemon.side_services import attach
from indus.gateway.hooks import (
    HANDLER_TIMEOUT_S,
    MESSAGE_CHARS,
    GatewayHooks,
    hook_context,
    load_hook_folder,
)
from indus.plugins.hooks import HookBus
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.gateway.core._kit import build_kit, dm
from tests.unit.gateway.daemon.ports import PortsDouble


def run[T](coro: Awaitable[T]) -> T:
    async def main() -> T:
        return await coro

    return asyncio.run(main())


def make_hook(
    home: Path,
    folder: str,
    events: list[str] | str | None,
    body: str,
    *,
    manifest: str | None = None,
) -> Path:
    where = home / "hooks" / folder
    where.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (where / "HOOK.yaml").write_text(manifest)
    else:
        (where / "HOOK.yaml").write_text(
            json.dumps({"name": folder, "description": f"{folder} hook", "events": events})
        )
    (where / "handler.py").write_text(textwrap.dedent(body))
    return where


RECORDER = """
    import json
    from pathlib import Path

    def handle(event_type, context):
        with open(Path(__file__).parent / "seen.jsonl", "a") as out:
            out.write(json.dumps({"event": event_type, "context": context}) + "\\n")
        return {"from": Path(__file__).parent.name, "event": event_type}
"""


def seen(folder: Path) -> list[dict[str, Any]]:
    path = folder / "seen.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_discovery_loads_good_folders_and_skips_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "h"
    make_hook(home, "a-good", ["agent:start"], RECORDER)
    make_hook(home, "b-no-events", [], RECORDER)
    make_hook(home, "c-bad-yaml", None, RECORDER, manifest="- just\n- a list\n")
    make_hook(home, "d-no-handle", ["agent:start"], "x = 1\n")
    make_hook(home, "e-import-error", ["agent:start"], "raise ImportError('nope')\n")
    (home / "hooks" / "f-no-handler").mkdir()
    (home / "hooks" / "f-no-handler" / "HOOK.yaml").write_text("events: [agent:start]\n")
    make_hook(home, "g-one-event", "session:start", RECORDER)
    (home / "hooks" / "stray.txt").write_text("not a folder")
    caplog.set_level(logging.WARNING, logger="indus.gateway.hooks")
    hooks = GatewayHooks(lambda: home)
    loaded = hooks.hooks_for(home)
    assert [h.name for h in loaded] == ["a-good", "g-one-event"]
    assert loaded[1].events == ("session:start",) and loaded[0].description == "a-good hook"
    text = caplog.text
    for folder in ("b-no-events", "c-bad-yaml", "d-no-handle", "e-import-error"):
        assert folder in text
    assert load_hook_folder(home / "hooks" / "f-no-handler") is None


def test_exact_and_wildcard_dispatch(tmp_path: Path) -> None:
    home = tmp_path / "h"
    every = make_hook(home, "every-command", ["command:*"], RECORDER)
    model = make_hook(home, "model-only", ["command:model"], RECORDER)
    bare = make_hook(home, "bare-kind", ["agent"], RECORDER)
    hooks = GatewayHooks(lambda: home)
    answers = run(hooks.emit("command:model", {"command": "model"}))
    assert answers == [
        {"from": "model-only", "event": "command:model"},
        {"from": "every-command", "event": "command:model"},
    ]
    run(hooks.emit("command:new", {}))
    run(hooks.emit("agent:start", {}))
    assert [e["event"] for e in seen(every)] == ["command:model", "command:new"]
    assert [e["event"] for e in seen(model)] == ["command:model"]
    assert seen(bare) == []  # a bare kind never matches "agent:start"


def test_async_handlers_and_answers(tmp_path: Path) -> None:
    home = tmp_path / "h"
    make_hook(
        home,
        "async-one",
        ["agent:end"],
        """
        async def handle(event_type, context):
            return {"seen": context["response"]}
    """,
    )
    make_hook(
        home,
        "quiet",
        ["agent:end"],
        """
        def handle(event_type, context):
            return None
    """,
    )
    hooks = GatewayHooks(lambda: home)
    assert run(hooks.emit("agent:end", {"response": "ok"})) == [{"seen": "ok"}]


def test_a_crashing_hook_is_logged_and_the_rest_still_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "h"
    make_hook(
        home,
        "a-crash",
        ["agent:start"],
        """
        def handle(event_type, context):
            raise ValueError("hook bug")
    """,
    )
    ok = make_hook(home, "b-ok", ["agent:start"], RECORDER)
    caplog.set_level(logging.WARNING, logger="indus.gateway.hooks")
    answers = run(GatewayHooks(lambda: home).emit("agent:start", {"message": "hi"}))
    assert answers == [{"from": "b-ok", "event": "agent:start"}]
    assert "a-crash failed on agent:start" in caplog.text and "hook bug" in caplog.text
    assert len(seen(ok)) == 1


def test_slow_hooks_are_abandoned_at_the_deadline(tmp_path: Path) -> None:
    assert HANDLER_TIMEOUT_S == 10.0
    home = tmp_path / "h"
    make_hook(
        home,
        "a-slow-sync",
        ["agent:step"],
        """
        import threading
        def handle(event_type, context):
            threading.Event().wait(5)
            return "late"
    """,
    )
    make_hook(
        home,
        "b-slow-async",
        ["agent:step"],
        """
        import asyncio
        async def handle(event_type, context):
            await asyncio.Event().wait()
    """,
    )
    make_hook(
        home,
        "c-fast",
        ["agent:step"],
        """
        def handle(event_type, context):
            return "fast"
    """,
    )
    hooks = GatewayHooks(lambda: home, timeout_s=0.2)

    async def timed() -> tuple[list[Any], float]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        answers = await hooks.emit("agent:step", {})
        return answers, loop.time() - started

    answers, took = run(timed())
    assert answers == ["fast"] and took < 3.0


def test_each_handler_gets_its_own_context_copy(tmp_path: Path) -> None:
    home = tmp_path / "h"
    make_hook(
        home,
        "a-mutator",
        ["session:start"],
        """
        def handle(event_type, context):
            context["platform"] = "changed"
            context.clear()
    """,
    )
    make_hook(
        home,
        "b-reader",
        ["session:start"],
        """
        def handle(event_type, context):
            return context.get("platform")
    """,
    )
    original = {"platform": "telegram"}
    assert run(GatewayHooks(lambda: home).emit("session:start", original)) == ["telegram"]
    assert original == {"platform": "telegram"}


def test_the_home_is_resolved_when_the_event_fires(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    make_hook(one, "hook", ["gateway:startup"], 'def handle(e, c):\n    return "one"\n')
    make_hook(two, "hook", ["gateway:startup"], 'def handle(e, c):\n    return "two"\n')
    current = [one]
    hooks = GatewayHooks(lambda: current[0])
    assert run(hooks.emit("gateway:startup")) == ["one"]
    current[0] = two
    assert run(hooks.emit("gateway:startup")) == ["two"]
    make_hook(two, "late", ["gateway:startup"], 'def handle(e, c):\n    return "late"\n')
    assert run(hooks.emit("gateway:startup")) == ["two"]  # loaded once per home
    assert [h.name for h in hooks.reload(two)] == ["hook", "late"]


def test_no_hooks_folder_is_fine(tmp_path: Path) -> None:
    assert run(GatewayHooks(lambda: tmp_path / "none").emit("agent:start", {})) == []


def test_context_shape() -> None:
    context = hook_context(
        platform="slack",
        user_id=None,
        chat_id="C1",
        thread_id=None,
        chat_type="group",
        session_id="s",
        message="x" * 600,
    )
    assert context == {
        "platform": "slack",
        "user_id": "",
        "chat_id": "C1",
        "thread_id": "",
        "chat_type": "group",
        "session_id": "s",
        "message": "x" * MESSAGE_CHARS,
    }


# -- the daemon bridge on a real pipeline ---------------------------------------------------------


def test_agent_start_end_and_command_hooks_through_the_daemon(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    recorder = make_hook(scope.home, "recorder", ["agent:*", "gateway:startup"], RECORDER)
    make_hook(
        scope.home,
        "guard",
        ["command:model"],
        """
        def handle(event_type, context):
            return {"decision": "deny", "message": "Models are fixed here."}
    """,
    )

    async def main() -> None:
        bus = HookBus(clock=fake_clock)
        kit = await build_kit(scope, fake_clock, tmp_path, text_turn("hello back"), hooks=bus)
        services = await attach(PortsDouble(scope, pipeline=kit.pipeline, hook_bus=bus))
        try:
            await services.started(["telegram"])
            await kit.pipeline.accept(dm("hello there"))
            await kit.until(lambda: "hello back" in kit.wire())
            await kit.until(lambda: any(e["event"] == "agent:end" for e in seen(recorder)))
            denied = await kit.pipeline.commands.dispatch_idle(dm("/model"), kit.key())
            assert denied == "Models are fixed here."
        finally:
            await services.stop()
            await kit.close()
        events = {e["event"]: e["context"] for e in seen(recorder)}
        assert events["gateway:startup"]["platforms"] == ["telegram"]
        start = events["agent:start"]
        assert start["platform"] == "telegram" and start["message"] == "hello there"
        assert start["chat_type"] == "dm" and start["thread_id"] == ""
        assert start["session_id"] and start["user_id"] and start["chat_id"]
        end = events["agent:end"]
        assert end["response"] == "hello back" and end["session_id"] == start["session_id"]
        assert {"model", "provider"} <= set(end)
        assert not bus.has("post_gateway_admission")  # the bridge was undone on stop

    run(main())
