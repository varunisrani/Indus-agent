"""P06-13: what a resume restores, and the per-terminal breadcrumb."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from indus.cli import resume
from indus.foundation.scope import Scope
from indus.safety.approvals.decision import APPROVALS, session_key
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    here = os.getcwd()
    yield
    os.chdir(here)


def with_terminal(scope: Scope, ident: str | None) -> Scope:
    changes: dict[str, str | None] = {resume.TERMINAL_ENV: ident}
    for var in ("TMUX_PANE", "ZELLIJ_PANE_ID", "KITTY_WINDOW_ID", "WEZTERM_PANE"):
        changes[var] = None
    changes["TERM_SESSION_ID"] = None
    changes["WT_SESSION"] = None
    return scope.with_env(scope.env.with_overlay(changes))


def no_tty(_fd: int) -> str:
    raise OSError("not a tty")


# -- cwd -------------------------------------------------------------------------------------------


def test_cwd_restored(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    outcome = resume.restore_cwd(str(target), skip=False)
    assert outcome.action == "moved"
    assert Path.cwd() == target.resolve()
    assert outcome.message and str(target) in outcome.message


def test_cwd_not_restored_when_skipped(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    here = Path.cwd()
    assert resume.restore_cwd(str(target), skip=True).action == "kept"
    assert Path.cwd() == here


def test_cwd_missing_dir_warns_and_stays(tmp_path: Path) -> None:
    here = Path.cwd()
    outcome = resume.restore_cwd(str(tmp_path / "gone"), skip=False)
    assert outcome.action == "stayed" and outcome.message and "gone" in outcome.message
    assert Path.cwd() == here


def test_cwd_already_there_says_nothing(tmp_path: Path) -> None:
    os.chdir(tmp_path)
    outcome = resume.restore_cwd(str(tmp_path), skip=False)
    assert outcome.action == "kept" and outcome.message is None


def test_no_recorded_cwd_is_a_no_op() -> None:
    assert resume.restore_cwd(None, skip=False).action == "kept"


# -- breadcrumbs -----------------------------------------------------------------------------------


def test_breadcrumb_per_terminal_id(scope: Scope, tmp_path: Path) -> None:
    one = with_terminal(scope, "pane-1")
    two = with_terminal(scope, "pane-2")
    path = resume.write_breadcrumb(one, "s_one", tmp_path, ttyname=no_tty)
    resume.write_breadcrumb(two, "s_two", tmp_path, ttyname=no_tty)
    assert path is not None and path.parent == scope.home / resume.BREADCRUMB_DIR
    assert json.loads(path.read_text(encoding="utf-8"))["session_id"] == "s_one"
    assert resume.read_breadcrumb(one, ttyname=no_tty) == "s_one"
    assert resume.read_breadcrumb(two, ttyname=no_tty) == "s_two"


def test_breadcrumb_rewritten_on_every_session(scope: Scope) -> None:
    one = with_terminal(scope, "pane-1")
    resume.write_breadcrumb(one, "s_first", ttyname=no_tty)
    resume.write_breadcrumb(one, "s_second", ttyname=no_tty)
    assert resume.read_breadcrumb(one, ttyname=no_tty) == "s_second"


def test_breadcrumb_needs_a_terminal(scope: Scope) -> None:
    nobody = with_terminal(scope, None)
    assert resume.terminal_id(nobody.env, ttyname=no_tty) is None
    assert resume.write_breadcrumb(nobody, "s1", ttyname=no_tty) is None
    assert resume.read_breadcrumb(nobody, ttyname=no_tty) is None


def test_breadcrumb_terminal_from_tty_then_pane(scope: Scope) -> None:
    env = with_terminal(scope, None).env
    named = resume.terminal_id(env, ttyname=lambda fd: "/dev/pts/7")
    assert named in (None, "tty-dev-pts-7")  # only when a test stream is a real terminal
    pane = env.to_dict() | {"TMUX_PANE": "%3"}
    assert resume.terminal_id(pane, ttyname=no_tty) == "tmux_pane-3"


def test_breadcrumb_off_by_config(scope: Scope) -> None:
    off = with_terminal(scope, "pane-1").with_config(
        DictConfig({"session": {"terminal_continue": False}})
    )
    assert resume.write_breadcrumb(off, "s1", ttyname=no_tty) is None
    on = with_terminal(scope, "pane-1")
    resume.write_breadcrumb(on, "s1", ttyname=no_tty)
    assert resume.read_breadcrumb(off, ttyname=no_tty) is None


def test_breadcrumb_stale_and_broken_are_ignored(scope: Scope, fake_clock: FakeClock) -> None:
    one = with_terminal(scope, "pane-1")
    path = resume.write_breadcrumb(one, "s1", ttyname=no_tty)
    assert path is not None
    fake_clock.advance(timedelta(days=31).total_seconds())
    assert resume.read_breadcrumb(one, ttyname=no_tty) is None
    path.write_text("not json", encoding="utf-8")
    assert resume.read_breadcrumb(one, ttyname=no_tty) is None


def test_terminal_id_is_file_safe(scope: Scope) -> None:
    env = with_terminal(scope, "../../etc/passwd").env
    ident = resume.terminal_id(env, ttyname=no_tty)
    assert ident is not None and ident.startswith("id-") and "/" not in ident
    assert (scope.home / ident).parent == scope.home


# -- switches --------------------------------------------------------------------------------------


def test_switches_rehydrated(scope: Scope) -> None:
    key = session_key(scope, "s_yolo")
    APPROVALS.clear_session(key)
    try:
        assert resume.rearm_auto_approve(scope, "s_yolo", {"model_config": '{"yolo": true}'})
        assert APPROVALS.auto_approve(key)
        assert not resume.rearm_auto_approve(scope, "s_other", {"model_config": "{}"})
        assert not APPROVALS.auto_approve(session_key(scope, "s_other"))
    finally:
        APPROVALS.clear_session(key)


# -- resume_session (indus -c and the REPL's /resume) ----------------------------------------------


def _session_with_launch(scope: Scope, sid: str, where: Path, **config: object) -> None:
    from indus.cli.session_select import LAUNCH_KEY, LaunchPolicy
    from indus.state import registry, sessions

    policy = LaunchPolicy(cwd=str(where), model="m", reasoning="low", toolsets=("web",))
    handle = registry.acquire(scope)
    try:
        sessions.create(
            handle, sid, "cli", cwd=str(where), model_config={LAUNCH_KEY: policy.record(), **config}
        )
    finally:
        registry.release(handle)


def test_resume_session_restores_everything(scope: Scope, tmp_path: Path) -> None:
    from indus.state import registry

    where = tmp_path / "proj"
    where.mkdir()
    sid = "20260101_090000_res001"
    _session_with_launch(scope, sid, where, reasoning_effort="High", yolo=True)
    pinned = with_terminal(scope, "pane-r")
    handle = registry.acquire(scope)
    try:
        got = resume.resume_session(pinned, sid, store=handle)
    finally:
        registry.release(handle)
        APPROVALS.clear_session(session_key(scope, sid))
    assert got.reasoning == "high" and got.policy.reasoning == "high"
    assert got.toolsets == ("web",) and got.auto_approve is True
    assert got.cwd.action == "moved" and Path.cwd() == where.resolve()
    assert got.policy.cwd == str(Path.cwd())
    assert resume.read_breadcrumb(pinned) == sid
    assert any("Auto-approve" in line for line in got.notices)


def test_resume_session_without_restore_and_with_a_host(scope: Scope, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from indus.state import registry

    where = tmp_path / "proj"
    where.mkdir()
    sid = "20260101_090000_res002"
    _session_with_launch(scope, sid, where)
    handle = registry.acquire(scope)

    async def load(session_id: str) -> list[str]:
        return ["row-1", "row-2"]

    host = SimpleNamespace(
        service=SimpleNamespace(store=SimpleNamespace(handle=handle, load=load)),
        call=lambda coro: __import__("asyncio").run(coro),
    )
    here = Path.cwd()
    try:
        got = resume.resume_session(scope, sid, host=host, restore=False, breadcrumb=False)
    finally:
        registry.release(handle)
    assert Path.cwd() == here and got.cwd.action == "kept"
    assert got.reasoning == "low" and got.history == ["row-1", "row-2"]
    assert got.auto_approve is False


def test_resume_session_unknown_id(scope: Scope) -> None:
    from indus.state import registry

    handle = registry.acquire(scope)
    try:
        got = resume.resume_session(scope, "20990101_000000_none00", store=handle)
    finally:
        registry.release(handle)
    assert got.row is None and got.policy is None and got.reasoning is None
