"""``computer_use`` (P10-20): driver mapping, captures, approvals, hard refusals, probe, doctor,
crash recovery. The driver is ``tests/fakes/desktop_driver.py`` run as a real child process."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.tools import toolsets
from indus.tools.builtin.computer_use import driver as drv
from indus.tools.builtin.computer_use import tool as cu
from indus.tools.calls import MAX_ERROR_CHARS, is_envelope
from indus.tools.registry import ToolRegistry
from tests.fakes.broker import FakeBroker, Reply
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import terminal_scope

FAKE = Path(__file__).resolve().parents[4] / "fakes" / "desktop_driver.py"


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 19, tzinfo=UTC))


def _command(log: Path, *flags: str) -> str:
    return shlex.join([sys.executable, str(FAKE), "--log", str(log), *flags])


def _scope(
    tmp_path: Path,
    *flags: str,
    approvals: str | Mapping[str, Any] = "off",
    command: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> tuple[Scope, Path]:
    log = tmp_path / "driver.log"
    cmd = command if command is not None else _command(log, *flags)
    section: dict[str, Any] = {"driver_cmd": cmd, **(extra or {})}
    policy = {"mode": approvals} if isinstance(approvals, str) else dict(approvals)
    config = {"approvals": policy, "computer_use": section}
    return terminal_scope(tmp_path, config), log


def _log(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _ops(log: Path) -> list[str]:
    return [entry["op"] for entry in _log(log)]


def _run[T](work: Callable[[cu.ComputerUseTool], Awaitable[T]]) -> T:
    async def go() -> T:
        tool = cu.ComputerUseTool()
        try:
            return await asyncio.wait_for(work(tool), 60)
        finally:
            await tool.shutdown()

    return asyncio.run(go())


async def _call(tool: cu.ComputerUseTool, scope: Scope, args: Mapping[str, Any], **kw: Any) -> Any:
    kw.setdefault("session_id", "s1")
    kw.setdefault("run_context", "interactive")
    return await tool.handle(args, scope=scope, **kw)


def _body(result: Any) -> dict[str, Any]:
    assert isinstance(result, str), result
    return json.loads(result)


# -- mapping --------------------------------------------------------------------------------------


def test_click_maps_coordinates_and_button(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path)
    result = _run(
        lambda t: _call(t, scope, {"action": "click", "x": 120, "y": 340, "button": "right"})
    )
    body = _body(result)
    assert body["ok"] is True and body["action"] == "click" and body["message"] == "click done"
    assert _log(log) == [
        {"op": "hello", "args": {}},
        {"op": "click", "args": {"x": 120, "y": 340, "button": "right"}},
    ]


@pytest.mark.parametrize(
    ("args", "op", "sent"),
    [
        ({"action": "click", "element": 4}, "click", {"element": 4, "button": "left"}),
        ({"action": "double_click", "x": 5, "y": 6}, "double_click", {"x": 5, "y": 6,
                                                                       "button": "left"}),
        (
            {"action": "drag", "element": 1, "to_x": 300, "to_y": 40},
            "drag",
            {"from_element": 1, "to_x": 300, "to_y": 40, "button": "left"},
        ),
        ({"action": "scroll", "direction": "up", "amount": 5}, "scroll",
         {"direction": "up", "amount": 5}),
        ({"action": "scroll", "amount": 500}, "scroll", {"direction": "down", "amount": 50}),
        ({"action": "type", "text": "hello"}, "type", {"text": "hello"}),
        ({"action": "key", "keys": "ctrl+s"}, "key", {"keys": "ctrl+s"}),
        ({"action": "set_value", "element": 2, "value": "Blue"}, "set_value",
         {"element": 2, "value": "Blue"}),
        ({"action": "focus_app", "app": "Notes"}, "focus_app", {"name": "Notes"}),
        ({"action": "wait", "ms": 50}, "wait", {"ms": 50}),
        ({"action": "wait", "ms": 99_999}, "wait", {"ms": 30_000}),
        ({"action": "capture", "mode": "vision", "app": "Mail"}, "capture",
         {"mode": "vision", "app": "Mail"}),
    ],
)  # fmt: skip
def test_each_action_reaches_the_driver_as_its_op(
    tmp_path: Path, args: dict[str, Any], op: str, sent: dict[str, Any]
) -> None:
    scope, log = _scope(tmp_path, "--hang-on", "never")
    if op == "wait" and sent["ms"] > 2000:
        # the fake caps its sleep at 2 s; the tool allows the requested pause on top
        scope, log = _scope(tmp_path, extra={"action_timeout_s": 5})
    _run(lambda t: _call(t, scope, args))
    assert _log(log)[-1] == {"op": op, "args": sent}


def test_list_apps_returns_the_apps(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path)
    body = _body(_run(lambda t: _call(t, scope, {"action": "list_apps"})))
    assert body == {
        "ok": True,
        "action": "list_apps",
        "apps": [{"name": "Notes", "pid": 101}, {"name": "Terminal", "pid": 102}],
        "count": 2,
    }


# -- captures -------------------------------------------------------------------------------------


def test_som_capture_returns_the_envelope(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path)
    result = _run(lambda t: _call(t, scope, {"action": "capture"}, native_vision=True))
    assert is_envelope(result)
    text, image = result["content"]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert text["text"] == result["text_summary"]
    summary = result["text_summary"]
    assert summary.startswith("capture som 1280x800 app=Notes window='Untitled'")
    assert "[1] button 'Button 1' at [10, 20, 40, 16]" in summary
    saved = Path(summary.split("share it as MEDIA:")[1].rstrip(")").strip())
    assert saved.parent == scope.home / "cache" / "computer_use" and saved.suffix == ".png"
    assert saved.read_bytes().startswith(b"\x89PNG")
    if os.name != "nt":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_ax_capture_is_text_with_the_tree(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path)
    body = _body(_run(lambda t: _call(t, scope, {"action": "capture", "mode": "ax"})))
    assert body["mode"] == "ax" and body["total_elements"] == 3
    assert body["tree"].startswith("window 'Untitled'\n  button 'Button 1'")
    assert "screenshot_path" not in body and "vision_analysis" not in body
    assert body["elements"][0] == {
        "index": 1,
        "role": "button",
        "label": "Button 1",
        "bounds": [10, 20, 40, 16],
        "app": "Notes",
    }


class _Aux:
    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []

    async def call(self, task: str, messages: Any, **kw: Any) -> Any:
        self.calls.append((task, messages))
        if isinstance(self.answer, Exception):
            raise self.answer
        return type("R", (), {"text": self.answer})()


def test_capture_on_a_text_route_asks_the_vision_side_task(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path)
    aux = _Aux("A notes window with three buttons.")
    body = _body(
        _run(lambda t: _call(t, scope, {"action": "capture"}, native_vision=False, aux=aux))
    )
    assert body["vision_analysis"] == "A notes window with three buttons."
    assert Path(body["screenshot_path"]).exists() and len(body["elements"]) == 3
    ((task, messages),) = aux.calls
    assert task == "vision"
    assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_capture_without_any_vision_falls_back_to_the_element_list(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path)
    for aux in (None, _Aux(RuntimeError("down"))):
        body = _body(_run(lambda t, a=aux: _call(t, scope, {"action": "capture"}, aux=a)))
        assert "vision_analysis" not in body and len(body["elements"]) == 3
        assert "act on the numbered controls" in body["note"]


@pytest.mark.parametrize(
    ("flag", "words"),
    [("--tiny", "(2x2) was left out"), ("--bad-image", "screenshot was left out: the content")],
)
def test_unusable_screenshots_are_left_out(tmp_path: Path, flag: str, words: str) -> None:
    scope, _ = _scope(tmp_path, flag)
    result = _run(lambda t: _call(t, scope, {"action": "capture"}, native_vision=True))
    body = _body(result)  # text, never an envelope with an image the provider would refuse
    assert words in body["note"] and "screenshot_path" not in body
    assert len(body["elements"]) == 3 and words in body["summary"]


def test_long_element_lists_are_capped_and_spilled(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path, "--elements", "150")
    body = _body(_run(lambda t: _call(t, scope, {"action": "capture", "mode": "ax"})))
    assert len(body["elements"]) == cu.MAX_ELEMENTS and body["truncated_elements"] == 50
    spilled = json.loads(Path(body["elements_file"]).read_text())
    assert spilled["total_elements"] == 150 and len(spilled["elements"]) == 150
    assert "… 110 more" in body["summary"]


def test_screenshots_keep_only_the_newest(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path)

    async def many(t: cu.ComputerUseTool) -> None:
        for _ in range(cu.KEEP_FILES + 5):
            await _call(t, scope, {"action": "capture"}, native_vision=True)

    _run(many)
    kept = list((scope.home / "cache" / "computer_use").glob("screen-*"))
    assert len(kept) == cu.KEEP_FILES


# -- approvals ------------------------------------------------------------------------------------


def test_denied_approval_never_reaches_the_driver(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, approvals="manual")
    broker = FakeBroker(_clock(), default=Reply(choice="deny", reason="not on my screen"))

    async def work(t: cu.ComputerUseTool) -> Any:
        result = await _call(t, scope, {"action": "click", "x": 1, "y": 2}, broker=broker)
        assert t.driver_count() == 0
        return result

    body = _body(_run(work))
    assert "denied" in body["error"] and "not on my screen" in body["error"]
    assert body["action"] == "click"
    assert _ops(log) == []  # not even started
    (request,) = broker.requests
    assert request.kind == "approval"
    assert request.command == "computer_use: left click at (1, 2)"
    assert [c.value for c in request.choices] == ["once", "session", "deny"]


def test_session_approval_is_asked_once_per_action(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, approvals="manual")
    broker = FakeBroker(_clock(), default=Reply(choice="session"))

    async def work(t: cu.ComputerUseTool) -> None:
        for args in (
            {"action": "click", "element": 1},
            {"action": "click", "element": 2},
            {"action": "type", "text": "hi"},
        ):
            assert _body(await _call(t, scope, args, broker=broker))["ok"] is True

    _run(work)
    assert [r.command for r in broker.requests if r.kind == "approval"] == [
        "computer_use: left click control [1]",
        "computer_use: type 'hi'",
    ]
    assert _ops(log) == ["hello", "click", "click", "type"]


def test_looking_and_waiting_never_ask(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, approvals="manual")
    broker = FakeBroker(_clock(), default=Reply(choice="deny"))

    async def work(t: cu.ComputerUseTool) -> None:
        for action in ("capture", "list_apps", "wait"):
            result = await _call(t, scope, {"action": action, "ms": 10}, broker=broker)
            assert "error" not in (result if isinstance(result, str) else result["text_summary"])

    _run(work)
    assert broker.requests == []
    assert _ops(log) == ["hello", "capture", "list_apps", "wait"]


def test_nobody_to_ask_blocks_unless_the_policy_approves(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, approvals="manual")
    body = _body(_run(lambda t: _call(t, scope, {"action": "key", "keys": "enter"})))
    assert "approvals.unattended_mode" in body["error"] and _ops(log) == []
    cron = _body(
        _run(lambda t: _call(t, scope, {"action": "key", "keys": "enter"}, run_context="cron"))
    )
    assert "approvals.cron_mode" in cron["error"] and _ops(log) == []

    allowed, _ = _scope(
        tmp_path / "b",
        approvals={"mode": "manual", "unattended_mode": "approve"},
        command=_command(log),
    )
    ok = _body(_run(lambda t: _call(t, allowed, {"action": "key", "keys": "enter"})))
    assert ok["ok"] is True and _ops(log) == ["hello", "key"]


def test_auto_approve_runs_without_asking(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, approvals="manual")
    scope = scope.with_env(scope.env.with_overlay({"INDUS_YOLO_MODE": "1"}))
    broker = FakeBroker(_clock(), default=Reply(choice="deny"))
    body = _body(_run(lambda t: _call(t, scope, {"action": "click", "element": 3}, broker=broker)))
    assert body["ok"] is True and broker.requests == [] and _ops(log) == ["hello", "click"]


# -- hard refusals --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"action": "key", "keys": "cmd+shift+q"},
        {"action": "key", "keys": "Command-Option-Shift-Q"},
        {"action": "key", "keys": "ctrl+alt+del"},
        {"action": "key", "keys": "super+l"},
        {"action": "type", "text": "rm -rf /"},
    ],
)
def test_hard_refusals_come_before_approval(tmp_path: Path, args: dict[str, Any]) -> None:
    scope, log = _scope(tmp_path, approvals="manual")
    broker = FakeBroker(_clock(), default=Reply(choice="once"))
    body = _body(_run(lambda t: _call(t, scope, args, broker=broker)))
    assert body["error"].startswith("refused:")
    assert broker.requests == [] and _ops(log) == []


def test_blocked_combo_reads_aliases_and_ignores_harmless_keys() -> None:
    assert cu.blocked_combo("Control+Option+Delete") == frozenset({"ctrl", "alt", "delete"})
    assert cu.blocked_combo("ctrl+s") is None
    assert cu.blocked_combo("cmd+q") is None


# -- argument checks ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "words"),
    [
        ({}, "`action` is required"),
        ({"action": "screenshot"}, "unknown action 'screenshot' (did you mean capture?)"),
        ({"action": "dance"}, "unknown action 'dance'; use one of capture, list_apps, wait, click"),
        ({"action": "click"}, "give `element`"),
        ({"action": "click", "x": 3}, "give `element`"),
        ({"action": "drag", "element": 1}, "drag needs an end"),
        ({"action": "type"}, "type needs `text`"),
        ({"action": "key"}, "key needs `keys`"),
        ({"action": "set_value", "value": "x"}, "set_value needs `element`"),
        ({"action": "focus_app"}, "focus_app needs `app`"),
        ({"action": "capture", "mode": "full"}, "mode must be som, vision or ax"),
        ({"action": "scroll", "direction": "sideways"}, "direction must be one of"),
    ],
)
def test_bad_arguments_fail_before_anything_runs(
    tmp_path: Path, args: dict[str, Any], words: str
) -> None:
    scope, log = _scope(tmp_path, approvals="manual")
    broker = FakeBroker(_clock(), default=Reply(choice="once"))
    body = _body(_run(lambda t: _call(t, scope, args, broker=broker)))
    assert words in body["error"]
    assert broker.requests == [] and _ops(log) == []


# -- availability and doctor ----------------------------------------------------------------------


def _available(scope: Scope) -> bool:
    registry = ToolRegistry(clock=_clock())
    cu.register(registry, cu.ComputerUseTool())
    (entry,) = registry.entries(scope)
    return registry.available(entry, scope)


def test_missing_driver_leaves_the_tool_out_and_doctor_explains(tmp_path: Path) -> None:
    unset, _ = _scope(tmp_path / "a", command="")
    assert not _available(unset)
    (row,) = drv.doctor_rows(unset, platform="linux")
    assert row.severity == "info" and "no driver command is set" in row.message
    assert row.fix is not None and drv.CMD_KEY in row.fix and drv.CMD_ENV in row.fix

    missing, _ = _scope(tmp_path / "b", command="indus-no-such-desktop-driver --serve")
    assert not _available(missing)
    (row,) = drv.doctor_rows(missing, platform="linux")
    assert row.severity == "warn" and "'indus-no-such-desktop-driver --serve' was not found" in (
        row.message
    )

    present, _ = _scope(tmp_path / "c")
    assert _available(present)
    rows = drv.doctor_rows(present, platform="linux")
    assert [(r.id, r.severity) for r in rows] == [("computer_use", "ok")]
    assert "fake-desktop 0.1" in rows[0].message


def test_driver_not_ready_is_absent_and_says_why(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, "--not-ready", "no display to attach to")
    assert not _available(scope)
    (row,) = drv.doctor_rows(scope, platform="linux")
    assert row.severity == "warn" and "no display to attach to" in row.message
    body = _body(_run(lambda t: _call(t, scope, {"action": "capture"})))
    assert "could not start" in body["error"] and "no display to attach to" in body["error"]
    assert _ops(log).count("hello") >= 1


def test_doctor_permission_rows(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path, "--permissions", "ax=0,sr=1")
    rows = {r.id: r for r in drv.doctor_rows(scope, platform="darwin")}
    assert rows["computer_use.accessibility"].severity == "warn"
    fix = rows["computer_use.accessibility"].fix or ""
    assert "System Settings > Privacy & Security > Accessibility" in fix
    assert rows["computer_use.screen_recording"].severity == "ok"

    quiet, _ = _scope(tmp_path / "q")
    mac = {r.id: r.severity for r in drv.doctor_rows(quiet, platform="darwin")}
    assert mac == {
        "computer_use": "ok",
        "computer_use.accessibility": "info",
        "computer_use.screen_recording": "info",
    }
    assert [r.id for r in drv.doctor_rows(quiet, platform="linux")] == ["computer_use"]


def test_doctor_check_is_registered(tmp_path: Path) -> None:
    from indus.cli.doctor import DoctorContext, all_checks

    spec = next(s for s in all_checks() if s.id == "computer_use")
    findings = spec.run(DoctorContext(scope=terminal_scope(tmp_path)))
    assert [f.id for f in findings] == ["computer_use"]


def test_env_command_wins_and_never_reaches_the_child(tmp_path: Path) -> None:
    log = tmp_path / "env.log"
    scope, _ = _scope(tmp_path, command="indus-no-such-desktop-driver")
    scope = scope.with_env(scope.env.with_overlay({drv.CMD_ENV: _command(log)}))
    command = drv.driver_command(scope)
    assert command is not None and command.source == "env" and command.found
    created = drv.provider_for(scope)
    assert created is not None
    made = created.create(scope, "s9")
    assert isinstance(made, drv.StdioDriver)
    assert drv.CMD_ENV not in made.env and made.env.get("INDUS_SESSION_ID") == "s9"
    assert _body(_run(lambda t: _call(t, scope, {"action": "list_apps"})))["count"] == 2


# -- crashes and timeouts -------------------------------------------------------------------------


def test_a_crash_is_a_bounded_error_and_the_next_call_restarts(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, "--crash-once", "click")

    async def work(t: cu.ComputerUseTool) -> tuple[Any, Any]:
        first = await _call(t, scope, {"action": "click", "element": 1})
        assert t.driver_count() == 0
        second = await _call(t, scope, {"action": "click", "element": 1})
        return first, second

    first, second = _run(work)
    assert len(first) < MAX_ERROR_CHARS
    error = _body(first)["error"]
    assert "the desktop driver stopped" in error and "exit code 3" in error
    assert "fake driver: crashing on click" in error
    assert _body(second)["ok"] is True
    assert _ops(log) == ["hello", "click", "hello", "click"]


def test_a_hung_driver_times_out_and_is_killed(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, "--hang-on", "key", extra={"action_timeout_s": 0.5})

    async def work(t: cu.ComputerUseTool) -> tuple[Any, int | None]:
        await _call(t, scope, {"action": "list_apps"})
        slot = next(iter(t._slots.values()))  # pyright: ignore[reportPrivateUsage]
        assert isinstance(slot.driver, drv.StdioDriver)
        pid = slot.driver.pid
        result = await _call(t, scope, {"action": "key", "keys": "enter"})
        assert t.driver_count() == 0
        return result, pid

    result, pid = _run(work)
    assert "did not answer key within 0.5 s" in _body(result)["error"]
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_garbage_output_counts_as_a_crash(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path, "--garbage-on", "scroll")
    body = _body(_run(lambda t: _call(t, scope, {"action": "scroll"})))
    assert "not JSON" in body["error"]


def test_a_refusal_from_the_driver_keeps_it_running(tmp_path: Path) -> None:
    scope, log = _scope(tmp_path, "--refuse", "focus_app")

    async def work(t: cu.ComputerUseTool) -> Any:
        refused = await _call(t, scope, {"action": "focus_app", "app": "Notes"})
        assert t.driver_count() == 1
        await _call(t, scope, {"action": "list_apps"})
        return refused

    body = _body(_run(work))
    assert body == {"error": "cannot focus_app here", "action": "focus_app"}
    assert _ops(log) == ["hello", "focus_app", "list_apps"]


def test_release_stops_the_session_driver(tmp_path: Path) -> None:
    scope, _ = _scope(tmp_path)

    async def work(t: cu.ComputerUseTool) -> None:
        await _call(t, scope, {"action": "list_apps"}, session_id="a")
        await _call(t, scope, {"action": "list_apps"}, session_id="b")
        assert t.driver_count() == 2
        assert await t.release("a", scope) is True
        assert t.driver_count() == 1
        assert await t.release("a", scope) is False

    _run(work)


# -- plugin drivers -------------------------------------------------------------------------------


class _MemoryDriver(drv.DesktopDriver):
    name = "memory"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def capture(self, mode: drv.CaptureMode, *, app: str | None = None) -> drv.Capture:
        self.calls.append(("capture", mode))
        return drv.Capture(mode, 10, 10, tree="root", elements=(drv.Element(1, "button", "OK"),))

    async def click(self, target: drv.Target, *, button: str = "left") -> drv.Outcome:
        self.calls.append(("click", (target, button)))
        return drv.Outcome(True, "click", "clicked")

    async def double_click(self, target: drv.Target, *, button: str = "left") -> drv.Outcome:
        raise NotImplementedError

    async def drag(
        self, start: drv.Target, end: drv.Target, *, button: str = "left"
    ) -> drv.Outcome:
        raise NotImplementedError

    async def scroll(self, target: drv.Target, *, direction: str, amount: int) -> drv.Outcome:
        raise NotImplementedError

    async def type_text(self, text: str) -> drv.Outcome:
        raise NotImplementedError

    async def key(self, combo: str) -> drv.Outcome:
        raise NotImplementedError

    async def set_value(self, element: int, value: str) -> drv.Outcome:
        raise NotImplementedError

    async def list_apps(self) -> list[dict[str, Any]]:
        return []

    async def focus_app(self, name: str) -> drv.Outcome:
        raise NotImplementedError


def test_a_registered_plugin_driver_is_used_when_selected(tmp_path: Path) -> None:
    made: list[_MemoryDriver] = []

    def create(scope: Scope, session_id: str | None) -> drv.DesktopDriver:
        made.append(_MemoryDriver())
        return made[-1]

    drv.register_driver(drv.DriverProvider("memory", create, lambda s: drv.Readiness(True)))
    try:
        scope, _ = _scope(tmp_path, extra={"driver": "memory"})
        assert _available(scope)
        body = _body(_run(lambda t: _call(t, scope, {"action": "click", "x": 3, "y": 4})))
        assert body == {"ok": True, "action": "click", "message": "clicked"}
        assert made[0].calls == [("click", (drv.Target(None, 3, 4), "left"))]
        with pytest.raises(ValueError, match="already registered"):
            drv.register_driver(drv.DriverProvider("memory", create, lambda s: drv.Readiness(True)))
    finally:
        assert drv.unregister_driver("memory")
    gone, _ = _scope(tmp_path / "g", extra={"driver": "memory"})
    assert not _available(gone)
    (row,) = drv.doctor_rows(gone, platform="linux")
    assert row.severity == "warn" and "'memory'" in row.message


# -- schema and toolsets --------------------------------------------------------------------------


def test_schema_lists_the_eleven_actions() -> None:
    props = cu.SCHEMA["parameters"]["properties"]
    assert props["action"]["enum"] == list(cu.ACTIONS) and len(cu.ACTIONS) == 11
    assert props["mode"]["enum"] == ["som", "vision", "ax"]
    assert {"capture", "list_apps", "wait"} == cu.READ_ONLY_ACTIONS
    assert cu.SCHEMA["parameters"]["required"] == ["action"]


def test_toolset_core_membership_and_deferral() -> None:
    assert toolsets.resolve("computer_use") == ["computer_use"]
    assert "computer_use" in toolsets.CORE_TOOLS
    assert "computer_use" in toolsets.DEFERRED_BY_DEFAULT
    assert "computer_use" not in toolsets.resolve("coding")
    assert "computer_use" not in toolsets.resolve("indus-api-server")
