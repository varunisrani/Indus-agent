"""P23-20: ``indus uninstall`` against temp homes only (fake services, keychain and registry)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli import uninstall as un
from indus.cli.args import CliState
from indus.foundation.scope import Scope
from indus.service.gateway_manager import Completed, SystemdUser
from indus.service.gateway_units import UnitSpec
from indus.state import gateway_runtime

USER_LINES = (
    "# my settings\n"
    'export PATH="$HOME/.local/bin:$PATH"\n'
    "# indus is installed under ~/.indus\n"
    "# >>> indus >>> (my own note)\n"
    "alias ll='ls -l'\n"
)
BLOCK = '# >>> indus >>>\nexport PATH="$HOME/.local/bin:$PATH"\n# <<< indus <<<\n'


@dataclass
class FakeService:
    path: Path
    running: bool = True
    calls: list[str] = field(default_factory=list[str])
    present: bool = True

    def installed(self) -> bool:
        return self.present

    def state(self) -> Any:
        return type("State", (), {"running": self.running})()

    def stop(self) -> None:
        self.calls.append("stop")
        self.running = False

    def uninstall(self) -> None:
        self.calls.append("uninstall")
        self.present = False


class FakeKeychain:
    def __init__(self, names: list[str]) -> None:
        self.entries = set(names)

    def names(self) -> list[str]:
        return sorted(self.entries)

    def delete(self, name: str) -> bool:
        if name in self.entries:
            self.entries.remove(name)
            return True
        return False


class FakeWinPath:
    def __init__(self, value: str) -> None:
        self.value = value

    def read(self) -> str | None:
        return self.value

    def write(self, value: str) -> None:
        self.value = value


@dataclass
class World:
    machine: un.Machine
    user: Path
    root: Path
    code: Path
    services: dict[Path, FakeService]
    keychains: dict[Path, FakeKeychain]
    live: dict[Path, un.LiveGateway]
    stopped: list[int]
    userdata: Path

    def answers(self, *replies: str | None) -> Callable[[str], str | None]:
        queue = list(replies)

        def ask(prompt: str) -> str | None:
            assert queue, f"unexpected question: {prompt}"
            return queue.pop(0)

        return ask


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _snapshot(folder: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.is_symlink():
            found[str(path.relative_to(folder))] = path.read_text(
                encoding="utf-8", errors="replace"
            )
    return found


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    user = tmp_path / "user"
    root = user / ".indus"
    code = root / "indus-agent"
    (code / ".git").mkdir(parents=True)
    _write(code / "pyproject.toml", "[project]\nname = 'indus-agent'\n")
    _write(code / "apps" / "desktop" / "release" / "linux-unpacked" / "indus", "app")
    _write(root / "config.yaml", "model: {}\n")
    _write(root / ".env", "")
    _write(root / "sessions" / "s1.json", "{}")
    _write(root / "tools" / "uv-1-linux" / "uv", "uv")
    named = root / "profiles" / "work"
    _write(named / "profile.yaml", "")
    _write(user / ".bashrc", USER_LINES + "\n" + BLOCK)
    _write(user / ".zshrc", BLOCK + USER_LINES)
    _write(user / ".profile", USER_LINES)
    launcher = user / ".local" / "bin" / "indus"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(code / ".venv" / "bin" / "indus")
    userdata = user / ".config" / "Indus"
    _write(userdata / "window-state.json", "{}")
    services = {root: FakeService(user / ".config" / "systemd" / "user" / "indus-gateway.service")}
    keychains = {root: FakeKeychain(["OPENROUTER_API_KEY"]), named: FakeKeychain(["WORK_KEY"])}
    live: dict[Path, un.LiveGateway] = {}
    stopped: list[int] = []

    def stop_pid(gateway: un.LiveGateway, timeout: float) -> bool:
        stopped.append(gateway.pid)
        return True

    machine = un.Machine(
        env={"HOME": str(user)},
        platform="linux",
        home=root,
        root=root,
        profile_id="default",
        code_dir=code,
        install_kind="git",
        user_home=user,
        service_for=lambda home: services.get(home),
        secrets_for=lambda home, pid: keychains.get(home),
        gateway_live=lambda home: live.get(home),
        stop_pid=stop_pid,
        profiles=lambda: [("work", named)],
    )
    yield World(machine, user, root, code, services, keychains, live, stopped, userdata)


def _run(world: World, level: str = "keep-data", **kwargs: Any) -> int:
    ask = kwargs.pop("ask", None)
    interactive = kwargs.pop("interactive", ask is not None)
    options = un.Options(level=level, **kwargs)
    if ask is None:
        return un.run(world.machine, options, interactive=interactive)
    return un.run(world.machine, options, interactive=interactive, ask=ask)


# -- PATH block --------------------------------------------------------------------------------


def test_remove_path_block_only_takes_the_exact_block() -> None:
    result = un.remove_path_block(USER_LINES + BLOCK + "tail\n")
    assert result == (USER_LINES + "tail\n", 1)
    assert un.remove_path_block(USER_LINES) == (USER_LINES, 0)


def test_unterminated_block_is_left_alone() -> None:
    assert un.remove_path_block(USER_LINES + "# >>> indus >>>\nexport X=1\n") is None


def test_crlf_rc_block_is_recognised() -> None:
    text = "a\r\n# >>> indus >>>\r\nx\r\n# <<< indus <<<\r\nb\r\n"
    assert un.remove_path_block(text) == ("a\r\nb\r\n", 1)


def test_keep_data_removes_only_the_marked_blocks(world: World) -> None:
    assert _run(world, yes=True) == 0
    assert (world.user / ".bashrc").read_text(encoding="utf-8") == USER_LINES + "\n"
    assert (world.user / ".zshrc").read_text(encoding="utf-8") == USER_LINES
    assert (world.user / ".profile").read_text(encoding="utf-8") == USER_LINES


def test_unterminated_rc_is_untouched_and_noted(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = USER_LINES + "# >>> indus >>>\nexport X=1\n"
    _write(world.user / ".zprofile", broken)
    assert _run(world, yes=True) == 0
    assert (world.user / ".zprofile").read_text(encoding="utf-8") == broken
    assert "left it alone" in capsys.readouterr().out


def test_symlinked_rc_target_is_rewritten_and_the_link_kept(world: World) -> None:
    real = _write(world.user / "dotfiles" / "bashrc", USER_LINES + BLOCK)
    (world.user / ".bashrc").unlink()
    (world.user / ".bashrc").symlink_to(real)
    assert _run(world, yes=True) == 0
    assert (world.user / ".bashrc").is_symlink()
    assert real.read_text(encoding="utf-8") == USER_LINES


# -- levels ------------------------------------------------------------------------------------


def test_keep_data_removes_code_and_tools_and_keeps_the_home(world: World) -> None:
    assert _run(world, yes=True) == 0
    assert not world.code.exists()
    assert not (world.root / "tools").exists()
    assert not (world.user / ".local" / "bin" / "indus").is_symlink()
    assert (world.root / "config.yaml").is_file()
    assert (world.root / "sessions" / "s1.json").is_file()
    assert world.userdata.exists()
    assert world.keychains[world.root].names() == ["OPENROUTER_API_KEY"]
    assert world.services[world.root].calls == ["stop", "uninstall"]


def test_dry_run_changes_nothing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    before = _snapshot(world.user)
    for level in ("keep-data", "full", "gui", "data"):
        assert _run(world, level, dry_run=True) == 0
    assert _snapshot(world.user) == before
    assert (world.user / ".local" / "bin" / "indus").is_symlink()
    assert world.services[world.root].calls == []
    assert world.keychains[world.root].names() == ["OPENROUTER_API_KEY"]
    out = capsys.readouterr().out
    assert "nothing is changed" in out and str(world.code) in out


def test_unattended_full_never_removes_named_profiles(world: World) -> None:
    named = world.root / "profiles" / "work"
    assert _run(world, "full", yes=True) == 0
    assert (named / "profile.yaml").is_file()
    assert world.keychains[named].names() == ["WORK_KEY"]
    assert world.keychains[world.root].names() == []
    assert sorted(p.name for p in world.root.iterdir()) == ["profiles"]
    assert not world.userdata.exists()


def test_full_without_named_profiles_removes_the_home(world: World) -> None:
    world.machine.profiles = lambda: []
    assert _run(world, "full", yes=True) == 0
    assert not world.root.exists()


def test_keychain_is_emptied_on_full_only(world: World) -> None:
    assert _run(world, "keep-data", yes=True) == 0
    assert world.keychains[world.root].names() == ["OPENROUTER_API_KEY"]


# -- interactive -------------------------------------------------------------------------------


def test_interactive_default_is_keep_data_after_typed_yes(world: World) -> None:
    ask = world.answers("", "yes")
    assert _run(world, ask=ask) == 0
    assert not world.code.exists() and world.root.exists()


def test_interactive_full_with_profiles_removed_on_request(world: World) -> None:
    named = world.root / "profiles" / "work"
    world.services[named] = FakeService(named / "unit", running=False)
    ask = world.answers("2", "y", "yes")
    assert _run(world, ask=ask) == 0
    assert not world.root.exists()
    assert world.keychains[named].names() == []
    assert world.services[named].calls == ["uninstall"]


def test_interactive_full_keeps_profiles_by_default(world: World) -> None:
    ask = world.answers("2", "", "yes")
    assert _run(world, ask=ask) == 0
    assert (world.root / "profiles" / "work" / "profile.yaml").is_file()


@pytest.mark.parametrize("replies", [("1", "no"), ("2", "n", "YES please"), ("3",), ("cancel",)])
def test_anything_but_yes_cancels(
    world: World, replies: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(world.user)
    assert _run(world, ask=world.answers(*replies)) == 0
    assert _snapshot(world.user) == before
    assert "Cancelled." in capsys.readouterr().out


@pytest.mark.parametrize("at", [0, 1])
def test_end_of_input_cancels(world: World, at: int, capsys: pytest.CaptureFixture[str]) -> None:
    replies: list[str | None] = ["1", "yes"]
    replies[at] = None
    before = _snapshot(world.user)
    assert _run(world, ask=world.answers(*replies)) == 0
    assert _snapshot(world.user) == before
    assert "Cancelled." in capsys.readouterr().out


def test_invalid_choice_is_asked_again(world: World) -> None:
    ask = world.answers("9", "1", "yes")
    assert _run(world, ask=ask) == 0
    assert not world.code.exists()


def test_interactive_shows_profiles_with_a_running_marker(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.live[world.root / "profiles" / "work"] = un.LiveGateway(4242, None)
    _run(world, ask=world.answers("3"))
    out = capsys.readouterr().out
    assert "work (gateway running)" in out
    assert "1 stored key(s)" in out


def test_no_terminal_without_yes_is_guidance(world: World) -> None:
    before = _snapshot(world.user)
    assert _run(world, interactive=False) == 1
    assert _snapshot(world.user) == before


# -- ownership ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["nix", "docker", "desktop"])
@pytest.mark.parametrize("level", ["keep-data", "full", "gui"])
def test_managed_installs_refuse_code_levels(
    world: World, kind: str, level: str, capsys: pytest.CaptureFixture[str]
) -> None:
    world.machine.install_kind = kind
    before = _snapshot(world.user)
    assert _run(world, level, yes=True) == 1
    assert _snapshot(world.user) == before
    assert "indus uninstall --data" in capsys.readouterr().err


def test_tree_without_git_is_refused(world: World) -> None:
    (world.code / ".git").rmdir()
    assert _run(world, yes=True) == 1
    assert world.code.exists()


def test_data_level_works_on_a_managed_install(world: World) -> None:
    world.machine.install_kind = "nix"
    assert _run(world, "data", yes=True) == 0
    assert world.code.is_dir() and (world.root / "tools").is_dir()
    assert (world.root / "profiles" / "work" / "profile.yaml").is_file()
    assert not (world.root / "sessions").exists() and not (world.root / "config.yaml").exists()
    assert world.keychains[world.root].names() == []
    assert world.keychains[world.root / "profiles" / "work"].names() == ["WORK_KEY"]


def test_data_level_refuses_while_the_gateway_runs(world: World) -> None:
    world.live[world.root] = un.LiveGateway(99, None)
    assert _run(world, "data", yes=True) == 1
    assert (world.root / "sessions" / "s1.json").is_file()


def test_home_inside_the_code_dir_is_refused(world: World) -> None:
    world.machine.home = world.code / "home"
    world.machine.root = world.code / "home"
    assert _run(world, yes=True) == 1
    assert world.code.exists()


# -- gui ---------------------------------------------------------------------------------------


def test_gui_removes_only_the_desktop_pieces(world: World) -> None:
    assert _run(world, "gui", yes=True) == 0
    assert not (world.code / "apps" / "desktop" / "release").exists()
    assert not world.userdata.exists()
    assert (world.code / "pyproject.toml").is_file()
    assert (world.root / "config.yaml").is_file()
    assert (world.user / ".local" / "bin" / "indus").is_symlink()
    assert world.services[world.root].calls == []


def test_gui_summary_shape(world: World) -> None:
    summary = un.gui_summary(world.machine)
    assert set(summary) == {
        "gui_installed",
        "built_artifacts",
        "shortcuts",
        "userdata_dir",
        "userdata_exists",
        "agent_installed",
        "install_kind",
        "code_dir",
        "home",
    }
    assert summary["gui_installed"] is True and summary["agent_installed"] is True
    built = world.code / "apps" / "desktop" / "release" / "linux-unpacked"
    assert summary["built_artifacts"] == [str(built)]


# -- launchers, services, Windows --------------------------------------------------------------


def test_foreign_launcher_is_kept(world: World) -> None:
    launcher = world.user / ".local" / "bin" / "indus"
    launcher.unlink()
    _write(launcher, '#!/bin/sh\nexec /opt/other/indus "$@"\n')
    assert _run(world, yes=True) == 0
    assert launcher.is_file()


def test_launcher_script_naming_the_checkout_is_removed(world: World) -> None:
    launcher = world.user / ".local" / "bin" / "indus"
    launcher.unlink()
    mark = "# indus launcher: written by install.sh"
    _write(launcher, f'#!/bin/sh\n{mark}\nexec "{world.code}/.venv/bin/indus" "$@"\n')
    assert _run(world, yes=True) == 0
    assert not launcher.exists()


def test_manual_gateway_gets_a_planned_stop_then_sigterm_by_pid(world: World) -> None:
    del world.services[world.root]
    world.live[world.root] = un.LiveGateway(31337, 12)
    assert _run(world, yes=True) == 0
    assert world.stopped == [31337]
    record = json.loads(gateway_runtime.planned_stop_path(world.root).read_text(encoding="utf-8"))
    assert record["pid"] == 31337 and record["reason"] == "uninstall"


def test_real_systemd_unit_is_disabled_and_removed(world: World) -> None:
    env = {"HOME": str(world.user), "PATH": os.environ.get("PATH", "")}
    calls: list[tuple[str, ...]] = []

    def runner(args: Any) -> Completed:
        calls.append(tuple(args))
        return Completed(0, "", "")

    manager = SystemdUser(UnitSpec.build(world.root, env=env), env, runner)
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text("[Unit]\n", encoding="utf-8")
    world.machine.service_for = lambda home: manager if home == world.root else None
    assert _run(world, yes=True) == 0
    assert not manager.path.exists()
    assert ("--user", "disable", manager.spec.unit_file) in calls


def test_windows_user_path_entry_is_removed(world: World) -> None:
    entry = str(world.root / "bin")
    fake = FakeWinPath(f"C:\\Tools;{entry};{entry.upper()}\\;D:\\bin")
    world.machine.platform = "win32"
    world.machine.windows_path = fake
    exe = f"{world.code}\\.venv\\indus.exe"
    launcher = f'@echo off\nrem indus launcher written by install.ps1\n"{exe}" %*\n'
    _write(world.root / "bin" / "indus.cmd", launcher)
    assert _run(world, yes=True) == 0
    assert fake.value == "C:\\Tools;D:\\bin"
    assert not (world.root / "bin" / "indus.cmd").exists()


def test_start_menu_entry_naming_the_checkout_is_removed(world: World) -> None:
    menu = world.user / "menu"
    ours = _write(menu / "Indus.lnk", "")
    ours.write_bytes(b"L\x00\x00\x00" + str(world.code).encode("utf-16-le") + b"\x00\x00")
    world.machine.start_menu = menu
    assert _run(world, "gui", yes=True) == 0
    assert not ours.exists()


def test_start_menu_entry_of_another_app_is_kept(world: World) -> None:
    menu = world.user / "menu"
    other = _write(menu / "Indus.lnk", "")
    other.write_bytes(b"L\x00\x00\x00" + "C:\\Program Files\\Indus\\Indus.exe".encode("utf-16-le"))
    world.machine.start_menu = menu
    assert _run(world, "gui", yes=True) == 0
    assert other.exists()


def test_desktop_userdata_dirs_per_platform(tmp_path: Path) -> None:
    user = tmp_path
    assert un.desktop_userdata_dir({}, "linux", user) == user / ".config" / "Indus"
    mac = user / "Library" / "Application Support" / "Indus"
    assert un.desktop_userdata_dir({}, "darwin", user) == mac
    assert un.desktop_userdata_dir({"APPDATA": "/a"}, "win32", user) == Path("/a") / "Indus"
    override = {"INDUS_DESKTOP_USER_DATA_DIR": str(tmp_path / "ud")}
    assert un.desktop_userdata_dir(override, "linux", user) == tmp_path / "ud"


# -- the click command -------------------------------------------------------------------------


def _invoke(scope: Scope, world: World, args: list[str], monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(un, "MACHINE_FACTORY", lambda _scope: world.machine)
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    return CliRunner().invoke(app_mod.cli, args, obj=state)


def test_command_without_a_terminal_exits_1_and_removes_nothing(
    scope: Scope, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(world.user)
    result = _invoke(scope, world, ["uninstall"], monkeypatch)
    assert result.exit_code == 1
    assert "-y" in result.output
    assert _snapshot(world.user) == before


def test_command_gui_summary_prints_json(
    scope: Scope, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(scope, world, ["uninstall", "--gui-summary"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["userdata_exists"] is True


def test_command_levels_are_exclusive(
    scope: Scope, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(scope, world, ["uninstall", "--full", "--data"], monkeypatch)
    assert result.exit_code == 2


def test_command_yes_full(scope: Scope, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke(scope, world, ["uninstall", "-y", "--full"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert not world.code.exists()
    assert (world.root / "profiles" / "work").is_dir()


def test_levels_document_lists_every_flag() -> None:
    doc = Path(__file__).resolve().parents[3] / "docs" / "internals" / "uninstall-levels.md"
    text = doc.read_text(encoding="utf-8")
    for flag in ("--full", "--gui", "--data", "--gui-summary", "--dry-run", "-y"):
        assert f"`{flag}" in text, flag
    for level in un.LEVELS:
        assert level in text


def test_removed_named_profiles_take_their_alias_commands(world: World) -> None:
    alias = _write(world.user / ".local" / "bin" / "work", '#!/bin/sh\nexec indus -p work "$@"\n')
    world.machine.aliases_for = lambda pid: [alias] if pid == "work" else []
    assert _run(world, "full", yes=True) == 0
    assert alias.is_file(), "unattended full keeps named profiles and their aliases"


def test_interactive_full_removes_the_aliases_of_removed_profiles(world: World) -> None:
    alias = _write(world.user / ".local" / "bin" / "work", '#!/bin/sh\nexec indus -p work "$@"\n')
    world.machine.aliases_for = lambda pid: [alias] if pid == "work" else []
    assert _run(world, ask=world.answers("2", "yes", "yes")) == 0
    assert not alias.exists()
