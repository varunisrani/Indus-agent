"""Profiles under one root for the multiplex tests: the launch (default) scope and named homes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from tests.fakes.clock import FakeClock


def launch_scope(root: Path, clock: FakeClock, *, env: Mapping[str, str] | None = None) -> Scope:
    """The default profile at ``root`` as the launch scope, its env the isolated process env plus
    ``env`` (file secret store, never the keychain)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").touch()
    view = EnvView.from_process().with_overlay({"INDUS_HOME": str(root), **dict(env or {})})
    scope = make_scope(root, env=view, clock=clock)
    return scope.with_secrets(open_secret_store(root, "default", mode="file"))


def make_profile(root: Path, name: str, *, env_lines: Mapping[str, str] | None = None) -> Path:
    """A live named profile ``name`` (identity marker written), optionally with a ``.env``."""
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("security:\n  secret_store: file\n", encoding="utf-8")
    if env_lines:
        text = "".join(f"{key}={value}\n" for key, value in env_lines.items())
        (home / ".env").write_text(text, encoding="utf-8")
    return home
