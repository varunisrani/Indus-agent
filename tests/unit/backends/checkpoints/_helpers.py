"""Shared bits for the checkpoint tests: a store on a temp home and a git for the user's repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from indus.backends.checkpoints.store import ShadowStore
from tests.fakes.clock import FakeClock

__all__ = ["make_store", "user_git", "write"]


def make_store(home: Path, clock: FakeClock, **kwargs: object) -> ShadowStore:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    return ShadowStore(home / "checkpoints", env=env, clock=clock, **kwargs)  # pyright: ignore[reportArgumentType]


def user_git(repo: Path, *args: str) -> str:
    """git as the user would run it in ``repo`` (no machine config, a fixed identity)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    done = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True, timeout=30
    )
    return done.stdout


def write(root: Path, rel: str, text: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path
