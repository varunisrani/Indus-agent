"""Run the ``indus`` app in-process against a test scope and collect what it printed."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from tests.fakes.clock import FakeClock


@dataclass(frozen=True)
class Ran:
    code: int
    out: str
    err: str


def run_cli(capsys: pytest.CaptureFixture[str], scope: Scope, *argv: str, tty: bool = False) -> Ran:
    capsys.readouterr()
    state = CliState(scope=scope, stdin_isatty=lambda: tty, stdout_isatty=lambda: tty)
    code = app_mod.dispatch(list(argv), state)
    captured = capsys.readouterr()
    return Ran(code, captured.out, captured.err)


def root_scope(root: Path, clock: FakeClock, **env: str) -> Scope:
    """The default profile at ``root`` with a private HOME and PATH (never the real ones)."""
    user_home = root.parent / "user-home"
    user_home.mkdir(parents=True, exist_ok=True)
    overlay = {
        "INDUS_HOME": str(root),
        "HOME": str(user_home),
        "PATH": str(root.parent / "no-bin"),
        **env,
    }
    view = EnvView.from_process().with_overlay(overlay)
    scope = make_scope(root, env=view, clock=clock)
    return scope.with_secrets(open_secret_store(root, scope.profile_id, mode="file"))


def write_settings(scope: Scope, text: str) -> Path:
    path = config.config_path(scope)
    path.write_text(text, encoding="utf-8")
    loader.invalidate(path)
    return path
