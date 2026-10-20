"""P12-32: the ``indus --tui`` launcher, driven with a shell script standing in for Node."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.cli import chat as chat_mod
from indus.cli import tui_launch
from indus.cli.app import dispatch
from indus.cli.args import BARE_CONTINUE, ChatArgs, CliState, GlobalArgs, HostOverrides
from indus.cli.tui_launch import System
from indus.foundation.scope import Scope
from indus.state import messages, registry, sessions, titles

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stand-ins")

STAND_IN_NODE = """#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "${PRETEND_NODE_VERSION:-v24.9.0}"
  exit 0
fi
env > "$PRETEND_OUT/env"
pwd > "$PRETEND_OUT/cwd"
printf '%s\\n' "$@" > "$PRETEND_OUT/argv"
if [ -n "$PRETEND_SESSION" ]; then
  printf '{"session_id": "%s"}' "$PRETEND_SESSION" > "$INDUS_TUI_ACTIVE_SESSION_FILE"
fi
if [ -n "$PRETEND_SIGNAL" ]; then
  kill -"$PRETEND_SIGNAL" $$
fi
exit "${PRETEND_EXIT:-0}"
"""

STAND_IN_INDUS = """#!/bin/sh
printf '%s\\n' "$@" > "$PRETEND_OUT/indus-argv"
env > "$PRETEND_OUT/indus-env"
exit 7
"""

SID = "20261019_120000_tuitst"


def _script(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@dataclass
class Rig:
    """A temp machine: a fake ``node`` on PATH, a bundled renderer, an output folder."""

    root: Path
    bin: Path
    out: Path
    pkg: Path
    calls: list[list[str]] = field(default_factory=list[list[str]])
    build_code: int = 0

    def system(self, **changes: Any) -> System:
        def call(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
            self.calls.append(argv)
            if argv[-1] == "build":
                return self.build_code
            return tui_launch._call(argv, cwd, env)  # pyright: ignore[reportPrivateUsage]

        base: dict[str, Any] = {
            "platform": "linux",
            "package_dir": self.pkg,
            "checkout_root": None,
            "temp_dir": self.root / "tmp",
            "script_dir": self.bin,
            "cgroup_files": (self.root / "no-cgroup",),
            "pid": 4242,
            "call": call,
        }
        base.update(changes)
        return System(**base)

    def env_out(self) -> dict[str, str]:
        lines = (self.out / "env").read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    bin_dir = tmp_path / "bin"
    _script(bin_dir / "node", STAND_IN_NODE)
    _script(bin_dir / "indus", STAND_IN_INDUS)
    pkg = tmp_path / "pkg"
    (pkg / "_tui_dist").mkdir(parents=True)
    (pkg / "_tui_dist" / "entry.js").write_text("// bundle\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "tmp").mkdir()
    return Rig(root=tmp_path, bin=bin_dir, out=out, pkg=pkg)


def _state(scope: Scope, rig: Rig, *, tty: bool = True, **env: str) -> CliState:
    overlay = {
        "PATH": f"{rig.bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "PRETEND_OUT": str(rig.out),
        "NODE_OPTIONS": "",
        **env,
    }
    return CliState(
        scope=scope.with_env(scope.env.with_overlay(overlay)),
        overrides=HostOverrides(route=object()),
        stdin_isatty=lambda: tty,
        stdout_isatty=lambda: tty,
    )


@pytest.fixture(autouse=True)
def keep_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = tmp_path / "project"
    here.mkdir()
    monkeypatch.chdir(here)


def _launch(state: CliState, rig: Rig, args: GlobalArgs | None = None, **kw: Any) -> int | None:
    system = kw.pop("system", None) or rig.system()
    chat = kw.pop("chat", ChatArgs())
    return tui_launch.run(state, args or GlobalArgs(interface="tui"), chat, system=system, **kw)


# -- the choice ------------------------------------------------------------------------------------


class Settings:
    def __init__(self, interface: str) -> None:
        self.interface = interface

    def get(self, key: str) -> str | None:
        return self.interface if key == "display.interface" else None


@pytest.mark.parametrize(
    ("interface", "terminal", "env", "config", "expected"),
    [
        ("cli", True, {"INDUS_TUI": "1"}, "tui", False),
        ("tui", False, {}, None, True),
        (None, False, {"INDUS_TUI": "1"}, "tui", False),
        (None, True, {"INDUS_TUI": "1"}, None, True),
        (None, True, {}, "tui", True),
        (None, True, {}, "TUI ", True),
        (None, True, {}, "cli", False),
        (None, True, {"INDUS_TUI": "0"}, None, False),
        ("safe", True, {"INDUS_TUI": "1"}, "tui", False),
        ("bare", True, {}, "tui", False),
    ],
)
def test_choice_order(
    interface: Any, terminal: bool, env: dict[str, str], config: str | None, expected: bool
) -> None:
    settings = Settings(config) if config is not None else None
    if interface == "safe":
        args = GlobalArgs(safe_mode=True)
    elif interface == "bare":
        args = GlobalArgs(ignore_user_config=True)
    else:
        args = GlobalArgs(interface=interface)
    assert tui_launch.wants_tui(args, terminal=terminal, env=env, config=settings) is expected


def test_explicit_tui_wins_over_safe_mode() -> None:
    args = GlobalArgs(interface="tui", safe_mode=True)
    assert tui_launch.wants_tui(args, terminal=True, env={}, config=None)


def test_broken_config_reads_as_classic() -> None:
    class Broken:
        def get(self, key: str) -> str:
            raise RuntimeError(key)

    assert not tui_launch.wants_tui(GlobalArgs(), terminal=True, env={}, config=Broken())


def test_piped_run_with_tui_setting_stays_classic(scope: Scope, rig: Rig) -> None:
    state = _state(scope, rig, tty=False, INDUS_TUI="1")
    assert chat_mod.choose_interface(state, GlobalArgs()) == ("classic", None)
    state.stdin_isatty = lambda: True
    state.stdout_isatty = lambda: True
    assert chat_mod.choose_interface(state, GlobalArgs()) == ("tui", None)


# -- the environment -------------------------------------------------------------------------------


def test_env_is_pinned_and_stale_names_are_cleared(scope: Scope, rig: Rig) -> None:
    state = _state(
        scope,
        rig,
        INDUS_TUI_RESUME="20200101_000000_stale0",
        INDUS_TUI_YOLO="1",
        INDUS_TUI_CWD="/somewhere/else",
        INDUS_LAUNCH_CWD="/somewhere/else",
        NODE_ENV="development",
    )
    args = GlobalArgs(
        interface="tui",
        model="model-x",
        provider="prov",
        toolsets="web, files,web",
        skills=("a,b", "c"),
        checkpoints=True,
        pass_session_id=True,
    )
    chat = ChatArgs(max_turns=12, quiet=True)
    assert _launch(state, rig, args, chat=chat, query="hello there") == 0
    env = rig.env_out()
    here = str(Path.cwd())
    assert env["INDUS_HOME"] == str(scope.home)
    assert env["INDUS_TUI_CWD"] == here and env["INDUS_LAUNCH_CWD"] == here
    assert Path(rig.out.joinpath("cwd").read_text().strip()) == Path(here)
    assert env["NODE_ENV"] == "production"
    assert env["INDUS_TUI_BRIDGE_COMMAND"] == str(rig.bin / "indus")
    assert "INDUS_TUI_RESUME" not in env
    assert "INDUS_TUI_YOLO" not in env
    assert env["INDUS_MODEL"] == "model-x"
    assert env["INDUS_TUI_PROVIDER"] == "prov"
    assert env["INDUS_TUI_TOOLSETS"] == "web,files"
    assert env["INDUS_TUI_SKILLS"] == "a,b,c"
    assert env["INDUS_TUI_QUERY"] == "hello there"
    assert env["INDUS_TUI_MAX_TURNS"] == "12"
    assert env["INDUS_TUI_TOOL_PROGRESS"] == "off"
    assert env["INDUS_TUI_CHECKPOINTS"] == "1" and env["INDUS_TUI_PASS_SESSION_ID"] == "1"
    assert env["INDUS_TUI_ACTIVE_SESSION_FILE"].endswith("indus-tui-active-session-4242.json")
    argv = rig.out.joinpath("argv").read_text().split()
    assert argv == [str(rig.pkg / "_tui_dist" / "entry.js")]


def test_attach_by_default_and_local_keeps_the_bridge_child(scope: Scope, rig: Rig) -> None:
    """P17-19: the renderer gets the grant helper's program; ``--local`` exports the switch, and
    a stale switch from the shell never survives."""
    state = _state(scope, rig, INDUS_TUI_LOCAL="1")
    assert _launch(state, rig) == 0
    env = rig.env_out()
    assert env["INDUS_TUI_GRANT_COMMAND"] == str(rig.bin / "indus")
    assert "INDUS_TUI_LOCAL" not in env

    @dataclass(frozen=True)
    class LocalArgs(GlobalArgs):  # ``--local`` lands with P17-18's flag table
        local: bool = True

    assert _launch(_state(scope, rig), rig, LocalArgs(interface="tui")) == 0
    assert rig.env_out()["INDUS_TUI_LOCAL"] == "1"
    own = _state(scope, rig, INDUS_TUI_GRANT_COMMAND="/opt/indus gateway grant --json")
    assert _launch(own, rig) == 0
    assert rig.env_out()["INDUS_TUI_GRANT_COMMAND"] == "/opt/indus gateway grant --json"


def test_user_bridge_command_is_kept(scope: Scope, rig: Rig) -> None:
    state = _state(scope, rig, INDUS_TUI_BRIDGE_COMMAND="/opt/my-indus")
    assert _launch(state, rig) == 0
    assert rig.env_out()["INDUS_TUI_BRIDGE_COMMAND"] == "/opt/my-indus"


def test_heap_flag_added_once_and_user_choice_kept() -> None:
    assert tui_launch.with_heap(None, 8192) == "--max-old-space-size=8192"
    assert tui_launch.with_heap("--trace-warnings", 100) == (
        "--trace-warnings --max-old-space-size=100"
    )
    mine = "--max-old-space-size=2048 --enable-source-maps"
    assert tui_launch.with_heap(mine, 8192) == mine
    twice = tui_launch.with_heap(tui_launch.with_heap("", 8192), 8192)
    assert twice.count("--max-old-space-size") == 1


def test_heap_in_child_env_respects_user_value(scope: Scope, rig: Rig) -> None:
    state = _state(scope, rig, NODE_OPTIONS="--max-old-space-size=1234")
    assert _launch(state, rig) == 0
    assert rig.env_out()["NODE_OPTIONS"] == "--max-old-space-size=1234"


def test_cgroup_limit_lowers_the_heap(tmp_path: Path, scope: Scope, rig: Rig) -> None:
    v2 = tmp_path / "memory.max"
    v2.write_text(f"{4 * 2**30}\n", encoding="utf-8")
    limit = tui_launch.cgroup_limit((v2,))
    assert limit == 4 * 2**30
    size = tui_launch.heap_mb(limit)
    assert 1024 <= size < 4096
    state = _state(scope, rig)
    assert _launch(state, rig, system=rig.system(cgroup_files=(v2,))) == 0
    assert rig.env_out()["NODE_OPTIONS"] == f"--max-old-space-size={size}"


def test_cgroup_without_a_limit(tmp_path: Path) -> None:
    v2 = tmp_path / "memory.max"
    v1 = tmp_path / "memory.limit_in_bytes"
    v2.write_text("max\n", encoding="utf-8")
    assert tui_launch.cgroup_limit((v2, v1)) is None
    v2.write_text("", encoding="utf-8")
    v1.write_text(str(2**63 - 4096), encoding="utf-8")
    assert tui_launch.cgroup_limit((v2, v1)) is None
    v1.write_text("junk", encoding="utf-8")
    assert tui_launch.cgroup_limit((v2, v1, tmp_path / "missing")) is None
    assert tui_launch.heap_mb(None) == tui_launch.DEFAULT_HEAP_MB
    assert tui_launch.heap_mb(256 * 2**20) == 192
    assert tui_launch.heap_mb(64 * 2**30) == tui_launch.DEFAULT_HEAP_MB


# -- resume ----------------------------------------------------------------------------------------


def _session(scope: Scope, sid: str, *, title: str | None = None, talk: bool = True) -> None:
    store = registry.acquire(scope)
    try:
        sessions.create(store, sid, "cli", cwd=str(Path.cwd()), git_root=str(Path.cwd()))
        if title:
            titles.set_title(store, sid, title)
        if talk:
            messages.append(store, sid, {"role": "user", "content": "hi"})
    finally:
        registry.release(store)


def test_resume_id_is_resolved_for_this_run(scope: Scope, rig: Rig) -> None:
    _session(scope, SID, title="plans")
    state = _state(scope, rig)
    assert _launch(state, rig, GlobalArgs(interface="tui", resume="plans")) == 0
    assert rig.env_out()["INDUS_TUI_RESUME"] == SID
    assert _launch(state, rig, GlobalArgs(interface="tui", continue_=BARE_CONTINUE)) == 0
    assert rig.env_out()["INDUS_TUI_RESUME"] == SID


def test_resume_miss_stops_before_node(
    scope: Scope, rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state(scope, rig)
    assert _launch(state, rig, GlobalArgs(interface="tui", resume="nothing-like-it")) == 1
    assert "No session found" in capsys.readouterr().err
    assert not (rig.out / "env").exists()


def test_resume_hint_after_ctrl_c(
    scope: Scope, rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _session(scope, SID, title="plans")
    state = _state(scope, rig, PRETEND_SESSION=SID, PRETEND_EXIT="130")
    assert _launch(state, rig) == 130
    out = capsys.readouterr().out
    assert f"indus --tui -r {SID} -p {scope.profile_id}" in out
    assert 'indus --tui -c "plans"' in out


def test_no_hint_on_failure_or_empty_session(
    scope: Scope, rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _session(scope, SID)
    state = _state(scope, rig, PRETEND_SESSION=SID, PRETEND_EXIT="1")
    assert _launch(state, rig) == 1
    assert "indus --tui -r" not in capsys.readouterr().out
    other = "20261019_130000_empty0"
    _session(scope, other, talk=False)
    state = _state(scope, rig, PRETEND_SESSION=other)
    assert _launch(state, rig) == 0
    assert "indus --tui -r" not in capsys.readouterr().out


def test_session_file_is_removed(scope: Scope, rig: Rig) -> None:
    state = _state(scope, rig, PRETEND_SESSION=SID)
    assert _launch(state, rig) == 0
    file = Path(rig.env_out()["INDUS_TUI_ACTIVE_SESSION_FILE"])
    assert file.parent == rig.root / "tmp"
    assert not file.exists()


def test_planted_link_at_the_session_file_is_not_followed(scope: Scope, rig: Rig) -> None:
    victim = rig.root / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    (rig.root / "tmp" / "indus-tui-active-session-4242.json").symlink_to(victim)
    assert _launch(_state(scope, rig), rig) == 0
    assert victim.read_text(encoding="utf-8") == "keep me"


def test_the_dashboard_channel_file_is_used_and_kept(scope: Scope, rig: Rig) -> None:
    # P18-09: the Chat tab's PTY child names its channel's file; the renderer writes there.
    channel_file = rig.root / "channel.json"
    channel_file.write_text("", encoding="utf-8")
    state = _state(
        scope, rig, PRETEND_SESSION=SID, INDUS_TUI_DASHBOARD_SESSION_FILE=str(channel_file)
    )
    assert _launch(state, rig) == 0
    env = rig.env_out()
    assert env["INDUS_TUI_ACTIVE_SESSION_FILE"] == str(channel_file)
    assert "INDUS_TUI_DASHBOARD_SESSION_FILE" not in env
    assert SID in channel_file.read_text(encoding="utf-8")


def test_a_linked_or_relative_dashboard_file_is_not_used(scope: Scope, rig: Rig) -> None:
    victim = rig.root / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    link = rig.root / "link.json"
    link.symlink_to(victim)
    for given in (str(link), "relative.json"):
        state = _state(scope, rig, PRETEND_SESSION=SID, INDUS_TUI_DASHBOARD_SESSION_FILE=given)
        assert _launch(state, rig) == 0
        used = Path(rig.env_out()["INDUS_TUI_ACTIVE_SESSION_FILE"])
        assert used.parent == rig.root / "tmp"
        assert not used.exists()
    assert victim.read_text(encoding="utf-8") == "keep me"


# -- exit codes ------------------------------------------------------------------------------------


def test_exit_code_is_mirrored(scope: Scope, rig: Rig) -> None:
    assert _launch(_state(scope, rig, PRETEND_EXIT="3"), rig) == 3


def test_signal_death_is_128_plus_n(scope: Scope, rig: Rig) -> None:
    assert _launch(_state(scope, rig, PRETEND_SIGNAL="TERM"), rig) == 128 + 15
    assert tui_launch.exit_code_of(-2) == 130


def test_ctrl_c_in_the_launcher_reads_as_130(scope: Scope, rig: Rig) -> None:
    class Interrupted:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 0

        def send_signal(self, sig: int) -> None:
            pass

        def kill(self) -> None:
            pass

    child = Interrupted()
    system = rig.system(start=lambda argv, cwd, env: child)
    assert _launch(_state(scope, rig), rig, system=system) == 130
    assert child.waits == 2


def test_exit_42_runs_update_without_tui_names(
    scope: Scope, rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state(scope, rig, PRETEND_EXIT="42", INDUS_TUI_RESUME="old")
    assert _launch(state, rig, GlobalArgs(interface="tui", model="m")) == 7
    assert rig.out.joinpath("indus-argv").read_text().split() == ["update"]
    lines = rig.out.joinpath("indus-env").read_text().splitlines()
    names = {line.split("=", 1)[0] for line in lines}
    assert not names & set(tui_launch.LAUNCH_NAMES)
    assert "INDUS_TUI_CWD" not in names
    assert "update" in capsys.readouterr().err


# -- falling back to classic -----------------------------------------------------------------------


def test_missing_node_explains_and_falls_back(
    scope: Scope, rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = rig.root / "empty-bin"
    empty.mkdir()
    state = _state(scope, rig, PATH=str(empty))
    assert _launch(state, rig) is None
    err = capsys.readouterr().err
    assert "Node.js 24" in err and "indus --cli" in err


def test_old_node_is_refused(scope: Scope, rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    assert _launch(_state(scope, rig, PRETEND_NODE_VERSION="v18.20.1"), rig) is None
    assert "v18.20.1" in capsys.readouterr().err
    assert _launch(_state(scope, rig, PRETEND_NODE_VERSION="v23.1.0"), rig) == 0
    assert "built for Node.js 24" in capsys.readouterr().err


def test_windows_prints_the_wsl_notice(
    scope: Scope, rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _launch(_state(scope, rig), rig, system=rig.system(platform="win32")) is None
    assert "WSL2" in capsys.readouterr().err


def test_chat_falls_back_to_classic_without_a_renderer(
    scope: Scope, rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(tui_launch, "System", lambda: rig.system(package_dir=rig.root / "none"))
    monkeypatch.setattr(chat_mod._ChatRun, "run", lambda self: ran.append("classic") or 0)  # pyright: ignore[reportPrivateUsage]
    state = _state(scope, rig)
    assert dispatch(["--tui"], state) == 0
    assert ran == ["classic"]
    assert "not part of this installation" in capsys.readouterr().err


def test_chat_runs_the_renderer(scope: Scope, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui_launch, "System", lambda: rig.system())
    state = _state(scope, rig, PRETEND_EXIT="5")
    assert dispatch(["chat", "--tui", "-m", "picked", "-Q"], state) == 5
    env = rig.env_out()
    assert env["INDUS_MODEL"] == "picked" and env["INDUS_TUI_TOOL_PROGRESS"] == "off"


# -- finding the renderer --------------------------------------------------------------------------


def _checkout(root: Path, *, built: bool, tsx: bool = False) -> Path:
    app = root / "apps" / "tui"
    (app / "src").mkdir(parents=True)
    (app / "src" / "entry.tsx").write_text("// src\n", encoding="utf-8")
    (root / "packages" / "client" / "src").mkdir(parents=True)
    (root / "packages" / "client" / "src" / "index.ts").write_text("//\n", encoding="utf-8")
    if built:
        (app / "dist").mkdir()
        bundle = app / "dist" / "entry.js"
        bundle.write_text("// built\n", encoding="utf-8")
        later = (app / "src" / "entry.tsx").stat().st_mtime + 100
        os.utime(bundle, (later, later))
    if tsx:
        _script(app / "node_modules" / ".bin" / "tsx", "#!/bin/sh\nexit 0\n")
    return root


def test_renderer_order(rig: Rig, tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    (custom / "dist").mkdir(parents=True)
    (custom / "dist" / "entry.js").write_text("//\n", encoding="utf-8")
    checkout = _checkout(tmp_path / "repo", built=True)
    system = rig.system(checkout_root=checkout)
    found = tui_launch.find_renderer({"INDUS_TUI_DIR": str(custom)}, system)
    assert found is not None and found.argv == (str(custom / "dist" / "entry.js"),)
    found = tui_launch.find_renderer({}, system)
    assert found is not None and found.argv == (str(rig.pkg / "_tui_dist" / "entry.js"),)
    found = tui_launch.find_renderer({}, rig.system(checkout_root=checkout, package_dir=tmp_path))
    assert found is not None and found.argv[0].endswith("apps/tui/dist/entry.js")
    assert rig.calls == []


def test_stale_checkout_is_rebuilt(rig: Rig, tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "repo", built=True)
    _script(rig.bin / "pnpm", "#!/bin/sh\nexit 0\n")
    newer = checkout / "packages" / "client" / "src" / "index.ts"
    stamp = (checkout / "apps" / "tui" / "dist" / "entry.js").stat().st_mtime + 50
    os.utime(newer, (stamp, stamp))
    system = rig.system(checkout_root=checkout, package_dir=tmp_path)
    env = {"PATH": str(rig.bin)}
    assert tui_launch.find_renderer(env, system) is not None
    assert rig.calls and rig.calls[-1][-2:] == ["run", "build"]
    rig.build_code = 1
    rig.calls.clear()
    assert tui_launch.find_renderer({**env, "INDUS_TUI_FORCE_BUILD": "yes"}, system) is not None
    assert len(rig.calls) == 1


def test_unbuilt_checkout_with_failed_build_falls_back(
    rig: Rig, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = _checkout(tmp_path / "repo", built=False)
    _script(rig.bin / "pnpm", "#!/bin/sh\nexit 1\n")
    rig.build_code = 1
    system = rig.system(checkout_root=checkout, package_dir=tmp_path)
    assert tui_launch.find_renderer({"PATH": str(rig.bin)}, system) is None
    assert "not built" in capsys.readouterr().err


def test_dev_runs_sources_through_tsx(rig: Rig, tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "repo", built=False, tsx=True)
    found = tui_launch.find_renderer({}, rig.system(checkout_root=checkout), dev=True)
    assert found is not None and not found.via_node
    assert found.argv[1] == "src/entry.tsx" and found.cwd == checkout / "apps" / "tui"


def test_dev_refusals(rig: Rig, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bare = _checkout(tmp_path / "repo", built=False)
    system = rig.system(checkout_root=bare)
    assert tui_launch.find_renderer({}, system, dev=True) is None
    assert "tsx" in capsys.readouterr().err
    assert tui_launch.find_renderer({"INDUS_TUI_DIR": "/x"}, system, dev=True) is None
    assert "INDUS_TUI_DIR" in capsys.readouterr().err
    assert tui_launch.find_renderer({}, rig.system(), dev=True) is None


def test_node_versions() -> None:
    assert tui_launch.node_major("v24.9.0") == 24
    assert tui_launch.node_major("22.1.0\n") == 22
    assert tui_launch.node_major("not node") is None
    assert tui_launch.node_major(None) is None
