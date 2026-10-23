"""``service.host_shell`` (W50-b): the one definition both hosts run ``shell.exec`` through.

The working folder's order (the live session's folder, the profile's ``terminal.cwd``, the host's
own folder) and the refusals that stop a command before anything runs: a detector that cannot be
loaded, an environment that cannot be built, an empty command.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_NOT_FOUND, SHELL_REFUSED, TEXT_EMPTY
from indus.protocol.methods import ProtocolError
from indus.service import host_shell
from indus.service.session_service import SessionService
from tests.fakes.clock import FakeClock


class FakeService:
    """``agent(session_id)`` for the folder lookup: known ids answer their folder."""

    def __init__(self, folders: dict[str, Any]) -> None:
        self.folders = folders

    def agent(self, session_id: str) -> SimpleNamespace:
        if session_id not in self.folders:
            raise ProtocolError(SESSION_NOT_FOUND)
        fields = self.folders[session_id]
        return SimpleNamespace(
            options=SimpleNamespace(cwd=fields.get("cwd"), session_fields=fields.get("fields", {}))
        )


def service_of(folders: dict[str, Any]) -> SessionService:
    return cast(SessionService, FakeService(folders))


def configure_terminal(scope: Scope, folder: Path | str) -> None:
    (scope.home / "config.yaml").write_text(f"terminal:\n  cwd: {folder}\n", encoding="utf-8")


def test_the_live_sessions_folder_comes_first(scope: Scope, tmp_path: Path) -> None:
    work, other = tmp_path / "work", tmp_path / "other"
    work.mkdir()
    other.mkdir()
    configure_terminal(scope, other)
    service = service_of({"s1": {"cwd": work}, "s2": {"fields": {"cwd": str(work)}}})
    assert host_shell.command_folder(scope, service, "s1") == str(work)
    assert host_shell.command_folder(scope, service, "s2") == str(work)  # stored field only


def test_a_missing_or_unknown_session_falls_back_to_the_terminal_folder(
    scope: Scope, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    configure_terminal(scope, other)
    service = service_of({"gone": {"cwd": tmp_path / "vanished"}, "none": {}})
    for sid in ("gone", "none", "unknown", None):
        assert host_shell.command_folder(scope, service, sid) == str(other), sid
    assert host_shell.command_folder(scope, None, "gone") == str(other)


def test_no_folder_at_all_leaves_the_hosts_own(scope: Scope, tmp_path: Path) -> None:
    assert host_shell.command_folder(scope, None, None) is None
    configure_terminal(scope, tmp_path / "not-there")
    assert host_shell.command_folder(scope, service_of({}), "s1") is None
    configure_terminal(scope, ".")
    assert host_shell.command_folder(scope, None, None) is None


def run(scope: Scope, command: str, clock: FakeClock) -> Any:
    return asyncio.run(
        host_shell.run_host_command(
            scope, command, session_id=None, service=None, timeout=30.0, clock=clock
        )
    )


def test_an_empty_command_is_refused_before_anything(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(host_shell, "run_captured", lambda command, **_: ran.append(command))
    with pytest.raises(ProtocolError) as caught:
        run(scope, " \t ", fake_clock)
    assert caught.value.error is TEXT_EMPTY and ran == []


def test_a_detector_that_cannot_load_refuses(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    monkeypatch.setitem(sys.modules, "indus.safety.approvals", None)  # import fails
    assert host_shell.refusal(scope, "echo hi") == (
        "the safety check could not be loaded, so nothing ran"
    )
    with pytest.raises(ProtocolError) as caught:
        run(scope, f"touch {marker}", fake_clock)
    assert caught.value.error is SHELL_REFUSED and not marker.exists()


def test_an_environment_that_cannot_be_built_refuses(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import indus.safety.child_env as child_env_module

    def broken(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise OSError("no environment")

    marker = tmp_path / "ran"
    monkeypatch.setattr(child_env_module, "child_env", broken)
    with pytest.raises(ProtocolError) as caught:
        run(scope, f"touch {marker}", fake_clock)
    assert caught.value.error is SHELL_REFUSED and "OSError" in str(caught.value)
    assert not marker.exists()


def test_a_command_runs_in_the_chosen_folder(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    folder = tmp_path / "terminal"
    folder.mkdir()
    configure_terminal(scope, folder)
    ran = run(scope, "pwd", fake_clock)
    assert ran.code == 0 and Path(ran.stdout.strip()).resolve() == folder.resolve()
