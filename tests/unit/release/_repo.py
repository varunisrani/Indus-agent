"""A local "GitHub remote" for the release tooling tests: a bare ``origin`` and a maintainer's
clone, real ``git`` only, no user config and no network (P23-22, P23-23)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.release.changelog import contributor_key
from scripts.release.claims import Claim

FIXED_EPOCH = 1_792_400_000  # 2026-10-19T…Z


def mail(user: str) -> str:
    return user + "@" + "example.invalid"


def base_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Maintainer",
        "GIT_AUTHOR_EMAIL": mail("maintainer"),
        "GIT_COMMITTER_NAME": "Maintainer",
        "GIT_COMMITTER_EMAIL": mail("maintainer"),
        "LC_ALL": "C",
    }


class Remote:
    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.env = base_env(self.home)
        self.origin = tmp / "origin.git"
        self.run(tmp, "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin))
        self.clone = tmp / "clone"
        self.run(tmp, "clone", "--quiet", str(self.origin), str(self.clone))
        self.run(self.clone, "checkout", "--quiet", "-B", "main")
        (self.clone / "contributors").mkdir()

    def run(self, cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
        done = subprocess.run(
            ["git", *args], cwd=str(cwd), env={**self.env, **(env or {})},
            capture_output=True, text=True, check=False, timeout=60,
        )  # fmt: skip
        if done.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout.strip()

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        return self.run(self.clone, *args, env=env)

    def commit(self, subject: str, *, author: str = "maintainer", push: bool = True) -> str:
        path = self.clone / "work.txt"
        with open(path, "a", encoding="utf-8") as out:
            out.write(subject + "\n")
        self.git("add", "work.txt")
        who = {"GIT_AUTHOR_NAME": author.title(), "GIT_AUTHOR_EMAIL": mail(author)}
        self.git("commit", "--quiet", "-m", subject, env=who)
        if push:
            self.git("push", "--quiet", "origin", "HEAD:main")
        return self.git("rev-parse", "HEAD")

    def credit(self, author: str, name: str) -> None:
        key = contributor_key(mail(author))
        (self.clone / "contributors" / f"{key}.txt").write_text(name + "\n", encoding="utf-8")

    def tag(self, name: str, commit: str, message: str, *, epoch: int | None = None) -> None:
        env = {"GIT_COMMITTER_DATE": f"@{epoch} +0000"} if epoch is not None else None
        self.git("tag", "--annotate", name, commit, "--message", message, env=env)
        self.git("push", "--quiet", "origin", f"refs/tags/{name}")

    def claim(
        self,
        version: str,
        commit: str,
        *,
        attempt: int = 1,
        autopublish: bool = False,
        lanes: tuple[str, ...] = ("ci", "docker"),
        epoch: int = FIXED_EPOCH,
        kind: str = "stable",
    ) -> str:
        """Push an attempt ref the way release.py does (claim message, tagger time = epoch)."""
        ref = f"rc.{attempt}-v{version}"
        claim = Claim(kind, version, attempt, commit, autopublish, frozenset(lanes), epoch)
        self.tag(ref, commit, claim.message(), epoch=epoch)
        return ref

    def remote_tags(self) -> dict[str, str]:
        listed = self.run(self.clone, "ls-remote", "--tags", "origin")
        found: dict[str, str] = {}
        for line in listed.splitlines():
            sha, _tab, name = line.partition("\t")
            found[name.removeprefix("refs/tags/")] = sha
        return found

    def message(self, tag: str) -> dict[str, object]:
        self.git("fetch", "--quiet", "--force", "origin", f"+refs/tags/{tag}:refs/tags/{tag}")
        return json.loads(self.git("tag", "--list", tag, "--format=%(contents)"))
