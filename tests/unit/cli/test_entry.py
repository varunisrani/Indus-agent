"""P06-01: the entry point and its startup order."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.cli import app as app_mod
from indus.cli import entry
from indus.cli.args import CHAT_VALUE_FLAGS, FALLBACK_VALUE_FLAGS, CliState
from indus.cli.version import install_dir, install_method, version_lines
from indus.foundation import layout
from indus.foundation import log as logmod

ROOT = Path(__file__).resolve().parents[3]
VERSION_BUDGET_US = 150_000
HEAVY = ("pydantic", "click", "httpx", "rich", "prompt_toolkit", "indus.foundation.bootstrap")


@pytest.fixture(autouse=True)
def clean_process_state() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    factory = logging.getLogRecordFactory()
    path = list(sys.path)
    layout.reset_memo()
    yield
    logmod.shutdown()
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.setLogRecordFactory(factory)
    sys.path[:] = path
    layout.reset_memo()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    work = base / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n", encoding="utf-8")
    return base


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], CliState]]:
    calls: list[tuple[list[str], CliState]] = []

    def spy(argv: list[str], state: CliState) -> int:
        calls.append((list(argv), state))
        return 7

    monkeypatch.setattr(app_mod, "dispatch", spy)
    return calls


def env_for(root: Path, **extra: str) -> dict[str, str]:
    return {"INDUS_HOME": str(root), "PATH": os.environ.get("PATH", ""), **extra}


@pytest.mark.parametrize(
    "argv",
    [
        ["-p", "work", "chat", "-q", "hi"],
        ["chat", "-p", "work", "-q", "hi"],
        ["chat", "-q", "hi", "--profile", "work"],
        ["--profile=work", "chat", "-q", "hi"],
    ],
)
def test_profile_flag_anywhere_is_resolved_and_stripped(
    root: Path, seen: list[tuple[list[str], CliState]], argv: list[str]
) -> None:
    env = env_for(root)
    assert entry.main(argv, env=env) == 7
    args, state = seen[0]
    assert args == ["chat", "-q", "hi"]
    assert state.scope.profile_id == "work"
    assert env["INDUS_HOME"] == str(root / "profiles" / "work")
    assert state.process is False


def test_int_result_is_the_exit_code(root: Path, seen: list[object]) -> None:
    assert entry.main(["chat"], env=env_for(root)) == 7


def test_reasoning_value_is_not_read_as_the_command() -> None:
    flags = entry._value_flags()  # pyright: ignore[reportPrivateUsage]
    assert entry._first_command(["--reasoning", "high", "chat"], flags) == "chat"  # pyright: ignore[reportPrivateUsage]
    assert entry._first_command(["-m", "dashboard", "chat"], flags) == "chat"  # pyright: ignore[reportPrivateUsage]


def test_profile_after_a_value_flag(root: Path, seen: list[tuple[list[str], CliState]]) -> None:
    entry.main(["--reasoning", "high", "-p", "work", "chat"], env=env_for(root))
    args, state = seen[0]
    assert state.scope.profile_id == "work"
    assert args == ["--reasoning", "high", "chat"]


def test_missing_named_profile_exits_1(
    root: Path, seen: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert entry.main(["-p", "ghost", "chat"], env=env_for(root)) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "ghost" in err[0]
    assert seen == []


def test_profile_layer_failure_warns_and_uses_default(
    root: Path, seen: list[tuple[list[str], CliState]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.foundation import preparse

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("profile bug")

    monkeypatch.setattr(preparse, "_select", boom)
    assert entry.main(["-p", "work", "chat"], env=env_for(root)) == 7
    args, state = seen[0]
    assert state.scope.profile_id == "default"
    assert args == ["chat"]


def test_yolo_flag_reaches_env_and_scope(
    root: Path, seen: list[tuple[list[str], CliState]]
) -> None:
    work = root / "profiles" / "work"
    (work / ".env").write_text("INDUS_YOLO_MODE=0\n", encoding="utf-8")
    env = env_for(root)
    entry.main(["-p", "work", "--yolo", "chat"], env=env)
    _, state = seen[0]
    assert state.scope.env["INDUS_YOLO_MODE"] == "1"


def test_yolo_inside_a_value_is_not_the_flag() -> None:
    flags = entry._value_flags()  # pyright: ignore[reportPrivateUsage]
    assert entry.find_flag(["-z", "--yolo"], "--yolo", flags) is False
    assert entry.find_flag(["chat", "-q", "--yolo"], "--yolo", flags) is False
    assert entry.find_flag(["--", "--yolo"], "--yolo", flags) is False
    assert entry.find_flag(["-m", "x", "--yolo"], "--yolo", flags) is True


def test_env_view_published_into_the_given_env(
    root: Path, seen: list[tuple[list[str], CliState]]
) -> None:
    work = root / "profiles" / "work"
    (work / ".env").write_text("FROM_DOTENV=yes\n", encoding="utf-8")
    env = env_for(root, FROM_DOTENV="shell")
    entry.main(["-p", "work", "chat"], env=env)
    assert env["FROM_DOTENV"] == "yes"


def test_provider_keys_are_blocked_for_children(
    root: Path, seen: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.safety import child_env

    got: dict[str, list[str]] = {}
    monkeypatch.setattr(
        child_env, "register_blocked_names", lambda source, names: got.update({source: list(names)})
    )
    entry.main(["chat"], env=env_for(root))
    assert "OPENAI_API_KEY" in got["providers"]


def test_value_flags_fallback_used_when_app_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> object:
        raise RuntimeError("no app")

    monkeypatch.setattr(app_mod, "value_flag_sets", broken)
    assert entry._value_flags() == FALLBACK_VALUE_FLAGS | CHAT_VALUE_FLAGS  # pyright: ignore[reportPrivateUsage]


SPY = """
import importlib.abc, os, sys
seen = {}
class Spy(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "indus.safety.approvals" and "env" not in seen:
            seen["env"] = os.environ.get("INDUS_YOLO_MODE")
        return None
sys.meta_path.insert(0, Spy())
from indus.cli import app, entry
def dispatch(args, state):
    import indus.tools.builtin.terminal  # what a chat run imports
    print("approvals saw", seen.get("env"), "scope", state.scope.env.get("INDUS_YOLO_MODE"))
    return 0
app.dispatch = dispatch
sys.argv = ["indus", "--yolo", "chat"]
raise SystemExit(entry.main())
"""


def test_yolo_env_set_before_approval_import(root: Path, tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env["INDUS_HOME"] = str(root)
    out = subprocess.run(
        [sys.executable, "-c", SPY],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "approvals saw 1 scope 1" in out.stdout


# -- version ---------------------------------------------------------------------------------------


def test_version_fast_path_prints_the_block(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["-p", "work", "--version"], env={}) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Indus Agent ")
    assert out[1].startswith("Install method: ")
    assert out[2].startswith("Install directory: ")


def test_version_fast_path_imports_little_and_fast(tmp_path: Path) -> None:
    code = "from indus.cli.entry import main; raise SystemExit(main(['-V']))"
    out = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert out.returncode == 0
    assert out.stdout.startswith("Indus Agent ")
    modules: dict[str, int] = {}
    for line in out.stderr.splitlines():
        found = re.match(r"import time:\s+\d+ \|\s+(\d+) \|\s+(\S.*)$", line)
        if found:
            modules[found.group(2).strip()] = int(found.group(1))
    for heavy in HEAVY:
        assert heavy not in modules, f"{heavy} imported on the version path"
    assert modules["indus.cli.entry"] + modules.get("indus.cli.version", 0) < VERSION_BUDGET_US


def test_version_flag_with_other_args_takes_the_full_path(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert entry.main(["--version", "chat"], env=env_for(root)) == 0
    assert capsys.readouterr().out.startswith("Indus Agent ")


def test_install_method_order(tmp_path: Path) -> None:
    tree = tmp_path / "checkout"
    pkg = tree / "src" / "indus"
    pkg.mkdir(parents=True)
    (tree / "pyproject.toml").write_text("", encoding="utf-8")
    never = lambda _p: False  # noqa: E731
    assert install_method(pkg, exists=never) == "dev"
    (tree / ".git").mkdir()
    assert install_method(pkg, exists=never) == "git"
    assert install_method(pkg, exists=lambda p: p == "/.dockerenv") == "docker"
    (tree / ".install_method").write_text("Installer\n", encoding="utf-8")
    assert install_method(pkg, exists=never) == "installer"
    wheel = tmp_path / "site-packages" / "indus"
    wheel.mkdir(parents=True)
    assert install_method(wheel, exists=never) == "package"
    assert install_dir(wheel) == tmp_path / "site-packages"
    assert install_dir(pkg) == tree


def test_version_lines_shape() -> None:
    lines = version_lines()
    assert len(lines) == 4 and lines[3].startswith("Python: ")
