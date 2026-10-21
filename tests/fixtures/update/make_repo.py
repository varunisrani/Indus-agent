"""Temp git repositories with tagged releases for the updater tests (P23-17, P23-18).

``Upstream(tmp)`` makes a bare ``origin`` and a publishing work tree. ``release(version)`` commits
a minimal source tree (every startup file ``apply.CRITICAL_FILES`` names, a store schema, a lock)
and pushes an annotated ``v<version>`` tag; flags make a release whose startup file does not
compile (``broken_syntax``) or whose lock cannot resolve (``broken_lock``, read by the fake uv).
``clone(dest, at=)`` is the user's checkout. Real ``git`` only; nothing touches the network or the
user's git config (``env()`` pins an empty global config and a test identity).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from indus.cli.update.apply import CRITICAL_FILES

BROKEN_LOCK_MARK = "# unresolvable: the fake uv fails on this lock"
OFFICIAL_URL_TAIL = "indusagi/indus-agent.git"
OFFICIAL_URL = "https://github.com/" + OFFICIAL_URL_TAIL


def env(home: Path) -> dict[str, str]:
    """A git environment that reads no user or system config."""
    who = "Indus Test"
    mail = "test" + "@" + "example.invalid"
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": who,
        "GIT_AUTHOR_EMAIL": mail,
        "GIT_COMMITTER_NAME": who,
        "GIT_COMMITTER_EMAIL": mail,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def git(cwd: Path, *args: str, home: Path) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env(home),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


class Upstream:
    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "git-home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.origin = tmp / "remotes" / OFFICIAL_URL_TAIL
        self.origin.mkdir(parents=True)
        git(self.origin, "init", "--quiet", "--bare", "--initial-branch=main", home=self.home)
        self.work = tmp / "publisher"
        git(tmp, "clone", "--quiet", str(self.origin), str(self.work), home=self.home)
        git(self.work, "checkout", "--quiet", "-B", "main", home=self.home)

    def write_tree(
        self,
        version: str,
        *,
        broken_syntax: bool = False,
        broken_lock: bool = False,
        schema: int = 1,
        extra: dict[str, str] | None = None,
    ) -> None:
        for name in CRITICAL_FILES:
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f'"""{name} at {version}."""\nVERSION = "{version}"\n', encoding="utf-8"
            )
        if broken_syntax:
            (self.work / "src/indus/cli/app.py").write_text(
                "def broken(:\n<<<<<<< HEAD\n", encoding="utf-8"
            )
        schema_file = self.work / "src/indus/state/schema.py"
        schema_file.parent.mkdir(parents=True, exist_ok=True)
        schema_file.write_text(f"SCHEMA_VERSION = {schema}\n", encoding="utf-8")
        lock = f"version = 1\n# release {version}\n"
        if broken_lock:
            lock += BROKEN_LOCK_MARK + "\n"
        (self.work / "uv.lock").write_text(lock, encoding="utf-8")
        (self.work / "pyproject.toml").write_text(
            '[project]\nname = "indus-agent"\nversion = "0.0.0"\n', encoding="utf-8"
        )
        (self.work / ".gitignore").write_text(".venv*/\n", encoding="utf-8")
        for name, text in (extra or {}).items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        git(self.work, "add", "--all", home=self.home)
        git(self.work, "commit", "--quiet", "--allow-empty", "-m", message, home=self.home)
        git(self.work, "push", "--quiet", "origin", "main", home=self.home)
        return git(self.work, "rev-parse", "HEAD", home=self.home)

    def tag(self, name: str, commit: str | None = None) -> None:
        target = commit or git(self.work, "rev-parse", "HEAD", home=self.home)
        git(self.work, "tag", "-a", name, target, "-m", name, home=self.home)
        git(self.work, "push", "--quiet", "origin", f"refs/tags/{name}", home=self.home)

    def release(self, version: str, **flags: object) -> str:
        self.write_tree(version, **flags)  # type: ignore[arg-type]
        sha = self.commit(f"release {version}")
        self.tag(f"v{version}")
        return sha

    def clone(self, dest: Path, *, at: str | None = None, official: bool = True) -> Path:
        """The user's checkout. ``official``: ``origin`` names the official repository (git
        rewrites it to the local bare repo, so nothing reaches the network)."""
        git(dest.parent, "clone", "--quiet", str(self.origin), str(dest), home=self.home)
        if official:
            git(dest, "config", "remote.origin.url", OFFICIAL_URL, home=self.home)
            git(dest, "config", f"url.{self.origin}.insteadOf", OFFICIAL_URL, home=self.home)
        if at is not None:
            git(dest, "checkout", "--quiet", "--detach", at, home=self.home)
        return dest
