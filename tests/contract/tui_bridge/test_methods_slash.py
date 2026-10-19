"""Slash commands in process (P12-31, R05.12): owned, client-side and later commands, crashes and
timeouts kept apart from "not mine", busy refusal, catalogue and dispatch kinds."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.learning.skills.invoke import Expansion
from indus.protocol.errors import (
    SESSION_BUSY,
    SLASH_NOT_OWNED,
    SLASH_WORKER_FAILED,
    UNSUPPORTED_COMMAND,
)
from indus.tui_bridge import methods_slash
from tests.contract.tui_bridge._harness import open_rig
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn


class FakeSkills:
    """Stands in for the skill command table: one skill, ``demo``."""

    def names(self) -> dict[str, str]:
        return {"demo": "skill: a demo"}

    def run(self, text: str, **_kw: Any) -> Expansion | None:
        return Expansion(text=f"[demo skill]\n{text}", loaded=("demo",))


def no_skills(monkeypatch: pytest.MonkeyPatch, table: object = None) -> None:
    monkeypatch.setattr(methods_slash, "skill_table", lambda bridge, session_id: table)


def test_owned_client_and_later_commands(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_skills(monkeypatch)

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            named = await rig.ok("slash.exec", session_id=sid, command="/title Road map")
            assert named["output"].startswith("Title set: Road map")
            shown = await rig.ok("slash.exec", session_id=sid, command="title")
            assert shown["output"] == "Title: Road map"
            status = await rig.ok("slash.exec", session_id=sid, command="/status")
            assert sid in status["output"] and "Idle." in status["output"]
            client = await rig.err("slash.exec", session_id=sid, command="/quit")
            assert client["code"] == SLASH_NOT_OWNED.code and "runs it itself" in client["message"]
            alias = await rig.err("slash.exec", session_id=sid, command="/exit")
            assert alias["code"] == SLASH_NOT_OWNED.code
            later = await rig.err("slash.exec", session_id=sid, command="/cron list")
            assert later["code"] == SLASH_NOT_OWNED.code and "P14" in later["message"]
            unknown = await rig.err("slash.exec", session_id=sid, command="/nonesuch")
            assert unknown["code"] == UNSUPPORTED_COMMAND.code
            helped = await rig.ok("slash.exec", session_id=sid, command="/help tit")
            assert "/title" in helped["output"]

    asyncio.run(main())


def test_crash_is_not_not_mine(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_skills(monkeypatch)

    async def boom(*_args: object) -> Any:
        raise RuntimeError("handler exploded")

    async def slow(*_args: object) -> Any:
        await asyncio.Event().wait()  # never answers

    monkeypatch.setitem(methods_slash.HANDLERS, "version", boom)
    monkeypatch.setitem(methods_slash.HANDLERS, "usage", slow)
    monkeypatch.setattr(methods_slash, "MIN_TIMEOUT_S", 0.05)

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            rig.bridge.slash_timeout = 0.0
            sid = (await rig.ok("session.create"))["session_id"]
            crashed = await rig.err("slash.exec", session_id=sid, command="/version")
            assert crashed["code"] == SLASH_WORKER_FAILED.code
            assert "handler exploded" in crashed["message"]
            stuck = await rig.err("slash.exec", session_id=sid, command="/usage")
            assert stuck["code"] == SLASH_WORKER_FAILED.code and "0.05 s" in stuck["message"]

    asyncio.run(main())


def test_timeout_floor_is_five_seconds() -> None:
    assert methods_slash.MIN_TIMEOUT_S == 5.0


def test_busy_policy_rejects_while_a_turn_runs(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_skills(monkeypatch)
    slow = Turn((Text(("working",)), Stall(50.0), Finish("stop")))

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, slow) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            await rig.ok("prompt.submit", session_id=sid, text="go")
            refused = await rig.err("slash.exec", session_id=sid, command="/title x")
            assert refused["code"] == SESSION_BUSY.code
            allowed = await rig.ok("slash.exec", session_id=sid, command="/status")
            assert "A reply is running." in allowed["output"]
            queued = await rig.ok("slash.exec", session_id=sid, command="/queue then this")
            assert queued["output"].startswith("Queued (1")
            listed = await rig.ok("slash.exec", session_id=sid, command="/queue list")
            assert listed["output"] == "1. then this"
            stopped = await rig.ok("slash.exec", session_id=sid, command="/stop")
            assert stopped["output"] == "Stopped the running reply."
            view = rig.bridge.view(sid)
            assert view is not None and not view.queue

    asyncio.run(main())


def test_dispatch_kinds(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_skills(monkeypatch, FakeSkills())
    (scope.home / "config.yaml").write_text(
        "quick_commands:\n"
        "  hi:\n    type: exec\n    command: echo hello\n"
        "  nuke:\n    type: exec\n    command: rm -rf /\n"
        "  m:\n    type: alias\n    target: title Picked\n"
        "  st:\n    type: alias\n    target: hi\n",
        encoding="utf-8",
    )

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path, text_turn("one")) as rig:
            sid = (await rig.ok("session.create"))["session_id"]
            ran = await rig.ok("command.dispatch", session_id=sid, name="/hi")
            assert ran == {"kind": "exec", "output": "hello\n"}
            chained = await rig.ok("command.dispatch", session_id=sid, name="st")
            assert chained["kind"] == "exec" and chained["output"] == "hello\n"
            alias = await rig.ok("command.dispatch", session_id=sid, name="m", arg="now")
            assert alias == {"kind": "alias", "target": "/title Picked now"}
            refused = await rig.err("command.dispatch", session_id=sid, name="nuke")
            assert refused["data"]["name"] == "SHELL_REFUSED"
            skill = await rig.ok("command.dispatch", session_id=sid, name="demo", arg="go")
            assert skill == {"kind": "skill", "message": "[demo skill]\n/demo go"}
            builtin = await rig.ok("command.dispatch", session_id=sid, name="title", arg="Plan")
            assert builtin["kind"] == "exec" and "Plan" in builtin["output"]
            await rig.ok("prompt.submit", session_id=sid, text="hello there")
            await rig.turn_done()
            await rig.idle(sid)
            retry = await rig.ok("command.dispatch", session_id=sid, name="retry")
            assert retry["kind"] == "send" and retry["message"] == "hello there"
            missing = await rig.err("command.dispatch", session_id=sid, name="zzz")
            assert missing["code"] == UNSUPPORTED_COMMAND.code
            skill_exec = await rig.err("slash.exec", session_id=sid, command="/demo x")
            assert skill_exec["code"] == SLASH_NOT_OWNED.code

    asyncio.run(main())


def test_catalog_lists_builtins_quick_and_skills(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_skills(monkeypatch, FakeSkills())
    (scope.home / "config.yaml").write_text(
        "quick_commands:\n  gs:\n    type: exec\n    command: git status\n", encoding="utf-8"
    )

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            catalog = (await rig.ok("commands.catalog"))["commands"]
            by_name: dict[str, dict[str, Any]] = {entry["name"]: entry for entry in catalog}
            assert by_name["title"]["runs"] == "server" and by_name["title"]["kind"] == "builtin"
            assert by_name["quit"]["runs"] == "client" and "exit" in by_name["quit"]["aliases"]
            assert by_name["cron"]["runs"] == "later"
            assert by_name["gs"]["kind"] == "quick"
            assert by_name["demo"]["kind"] == "skill"
            assert "worktree" not in by_name  # not offered on this surface

    asyncio.run(main())


def test_every_server_command_is_in_the_registry() -> None:
    from indus.service.commands.registry import REGISTRY

    names: Callable[[str], object] = REGISTRY.resolve
    for name in (*methods_slash.HANDLERS, *methods_slash.CLIENT):
        found = names(name)
        assert found is not None, name


def test_handlers_never_import_the_cli() -> None:
    code = (
        "import sys; from indus.tui_bridge.dispatch import build_table; build_table(); "
        "print(sorted(m for m in sys.modules if m.startswith('indus.cli')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == "[]"
