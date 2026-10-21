"""Apply an update in the old process: snapshot, move, verify, rebuild, hand off (P23-18).

Order (each step only after the one before succeeded; R04.24):

1. snapshot — this profile's state through the backup code (``quick`` / ``full`` / ``off``); a
   failure warns and is recorded, it never stops the update;
2. stash — a dirty tree is stashed with untracked files under ``indus-update-autostash-<stamp>``;
3. move — the checkout goes to exactly the target commit (``main``: the ``main`` branch is set to
   it; releases: a detached checkout). A HEAD that would leave the history is kept on
   ``refs/indus/update-backups/<stamp>`` first;
4. syntax — the current interpreter compiles (never imports) the startup files of the new tree
   into a temp dir; a failure puts the old commit back and exits 1 naming the file;
5. venv — a relocatable ``.venv-next`` is built from the new lock (``uv sync --frozen``) and
   proven by ``.venv-next/bin/python -m indus --version`` in a child; a failure removes it, puts
   the old commit back and exits 1, the old venv untouched;
6. swap — ``.venv`` → ``.venv-prev`` (the previous one removed first), ``.venv-next`` → ``.venv``;
7. local edits go back on top of the new tree; a stash that does not apply stays in the stash list
   and the run ends with exit 1 naming it (after the finish step, so the install is consistent);
8. hand-off — a request file, then ``exec`` of ``<new venv python> -m indus update --finish``: the
   old interpreter never runs a pulled module.

Ctrl-C before the hand-off undoes the steps taken so far, in reverse, and exits 130.
"""

from __future__ import annotations

import contextlib
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from indus.cli.update import (
    EXIT_INTERRUPTED,
    REQUEST_SCHEMA,
    Console,
    Receipt,
    UpdatePaths,
    stamp_of,
)
from indus.cli.update.channels import ChannelChoice, Git, GitError, Target
from indus.foundation.fsio import atomic_write_json
from indus.foundation.versions import MAIN

__all__ = [
    "AUTOSTASH_PREFIX",
    "BACKUP_REF_PREFIX",
    "CRITICAL_FILES",
    "ApplyHooks",
    "ApplyRequest",
    "apply_update",
    "syntax_problem",
    "venv_python",
]

AUTOSTASH_PREFIX = "indus-update-autostash-"
BACKUP_REF_PREFIX = "refs/indus/update-backups/"
CRITICAL_FILES: tuple[str, ...] = (
    "src/indus/__init__.py",
    "src/indus/__main__.py",
    "src/indus/cli/__init__.py",
    "src/indus/cli/entry.py",
    "src/indus/cli/app.py",
    "src/indus/cli/args.py",
    "src/indus/cli/lazy.py",
    "src/indus/cli/version.py",
    "src/indus/cli/commands/update.py",
    "src/indus/foundation/__init__.py",
    "src/indus/foundation/bootstrap.py",
    "src/indus/foundation/preparse.py",
    "src/indus/foundation/home.py",
    "src/indus/foundation/scope.py",
    "src/indus/foundation/errors.py",
    "src/indus/foundation/versions.py",
)
"""Every ``indus`` start imports these; with the update package (the finish step runs it) they
are what the syntax check compiles."""
_UPDATE_PACKAGE = "src/indus/cli/update"
_VALIDATE_TIMEOUT_S = 120.0
_UV_TIMEOUT_S = 1800.0

type ChildRunner = Callable[..., subprocess.CompletedProcess[str]]


def venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _default_exec(path: str, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(path, list(argv), dict(env))


@dataclass
class ApplyHooks:
    """What the apply step needs from the outside world (tests replace each)."""

    snapshot: Callable[[str], str]
    """``mode`` (``quick``/``full``) → a description of what was saved; raises on failure."""
    uv: str | None
    """The ``uv`` executable, or None when there is none."""
    run: ChildRunner = subprocess.run
    execv: Callable[[str, Sequence[str], Mapping[str, str]], Any] = _default_exec


@dataclass
class ApplyRequest:
    paths: UpdatePaths
    profile_home: Path
    choice: ChannelChoice
    target: Target
    env: Mapping[str, str]
    now: Callable[[], datetime]
    backup_mode: str = "quick"
    backup_reason: str = ""
    extras: Sequence[str] = ()
    assume_yes: bool = False
    no_gateway_restart: bool = False
    finish_argv: Sequence[str] = field(default_factory=tuple[str, ...])
    """Extra arguments passed through to ``--finish`` (``-y``, ``--no-gateway-restart``)."""


class _Abort(Exception):
    """A step failed; the message is what the person reads. The exit code is 1."""


class _HandOffFailed(Exception):
    """The update is in place but its finish step could not be started (exit 1)."""


@dataclass
class _Progress:
    pre: str = ""
    pre_branch: str | None = None
    pre_main: str | None = None
    stash: str | None = None
    stash_label: str = ""
    moved: bool = False
    built: bool = False
    swapped: bool = False
    had_venv: bool = False


def syntax_problem(tree: Path, files: Sequence[str] | None = None) -> tuple[str, str] | None:
    """Compile the startup files of ``tree`` (into a temp dir, never ``__pycache__``) with the
    running interpreter. ``(file, error)`` for the first failure (a missing file counts), else
    None."""
    names = list(CRITICAL_FILES if files is None else files)
    package = tree / _UPDATE_PACKAGE
    if files is None and package.is_dir():
        names += sorted(p.relative_to(tree).as_posix() for p in package.glob("*.py"))
    with tempfile.TemporaryDirectory(prefix="indus-syntax-") as scratch:
        for index, name in enumerate(dict.fromkeys(names)):
            source = tree / name
            if not source.is_file():
                return name, "the file is missing from the new release"
            try:
                py_compile.compile(
                    str(source), cfile=str(Path(scratch) / f"{index}.pyc"), doraise=True
                )
            except py_compile.PyCompileError as exc:
                return name, str(exc.msg).strip()
            except (OSError, ValueError) as exc:
                return name, f"could not read it: {exc}"
    return None


class _Applier:
    def __init__(
        self,
        git: Git,
        request: ApplyRequest,
        hooks: ApplyHooks,
        console: Console,
        receipt: Receipt,
    ) -> None:
        self.git = git
        self.req = request
        self.hooks = hooks
        self.out = console
        self.receipt = receipt
        self.p = _Progress()
        self.stamp = stamp_of(request.now())

    # -- steps -------------------------------------------------------------------------------------

    def snapshot(self) -> None:
        mode = self.req.backup_mode
        if mode == "off":
            reason = self.req.backup_reason or "updates.pre_update_backup is off"
            self.receipt.phase("snapshot", "skipped", reason)
            return
        self.out.say(f"→ Saving a {mode} snapshot of this profile before the update...")
        try:
            saved = self.hooks.snapshot(mode)
        except Exception as exc:  # a snapshot never stops an update
            self.out.warn(f"⚠ Pre-update snapshot failed ({exc}); continuing without it.")
            self.receipt.phase("snapshot", "failed", str(exc) or type(exc).__name__)
            return
        self.receipt.phase("snapshot", "ok", saved)

    def stash(self) -> None:
        if not self.git.dirty():
            self.receipt.phase("stash", "skipped", "no local changes")
            return
        label = f"{AUTOSTASH_PREFIX}{self.stamp}"
        self.git.run("stash", "push", "--include-untracked", "--message", label)
        self.p.stash = self.git.out("rev-parse", "refs/stash")
        self.p.stash_label = label
        self.out.say(f"→ Local changes put aside as {label}")
        self.receipt.phase("stash", "ok", label)

    def move(self) -> None:
        git, target = self.git, self.req.target
        self.p.pre = git.head()
        self.p.pre_branch = git.branch()
        self.p.pre_main = git.resolve(f"refs/heads/{MAIN}")
        if not git.is_ancestor(self.p.pre, target.commit):
            ref = f"{BACKUP_REF_PREFIX}{self.stamp}"
            git.run("update-ref", ref, self.p.pre)
            self.out.say(f"→ Commits not in {target.label} are kept on {ref}")
            self.receipt.phase("backup_ref", "ok", ref)
        self.out.say(f"→ Moving the checkout to {target.label} ({target.commit[:10]})...")
        self.p.moved = True
        if target.channel == MAIN:
            git.run("checkout", "--quiet", "-B", MAIN, target.commit)
        else:
            git.run("checkout", "--quiet", "--detach", target.commit)
        landed = git.head()
        if landed != target.commit:
            raise _Abort(f"the checkout landed on {landed[:10]}, not {target.commit[:10]}")
        self.receipt.phase("move", "ok", f"{self.p.pre[:10]} → {target.commit[:10]}")

    def syntax(self) -> None:
        problem = syntax_problem(self.req.paths.checkout)
        if problem is None:
            self.receipt.phase("syntax", "ok")
            return
        name, error = problem
        self.receipt.phase("syntax", "failed", f"{name}: {error}")
        raise _Abort(
            f"{self.req.target.label} has a broken startup file, so it was not installed:\n"
            f"  {name}\n    {error.splitlines()[0] if error else ''}"
        )

    def _child(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> str:
        """Run a child; return '' on success, else the tail of its output."""
        try:
            done = self.hooks.run(
                list(argv),
                cwd=str(self.req.paths.checkout),
                env=dict(env),
                timeout=timeout,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"{Path(argv[0]).name} did not finish within {timeout:g} s"
        except OSError as exc:
            return f"could not run {Path(argv[0]).name}: {exc}"
        self.out.child_output(f"$ {' '.join(argv)}\n{done.stdout}{done.stderr}")
        if done.returncode == 0:
            return ""
        lines = (done.stderr or done.stdout or "").strip().splitlines()
        return "\n".join(lines[-5:]) or f"exit status {done.returncode}"

    def _keeps_dev_group(self) -> bool:
        venv = self.req.paths.venv
        return any(venv.glob("lib/python*/site-packages/pytest-*.dist-info")) or any(
            venv.glob("Lib/site-packages/pytest-*.dist-info")
        )

    def venv(self) -> None:
        paths = self.req.paths
        uv = self.hooks.uv
        if uv is None:
            self.receipt.phase("venv", "failed", "uv not found")
            raise _Abort(
                "uv was not found (on PATH or under <home>/tools), so no environment "
                "could be built for the new code; the old one is kept."
            )
        shutil.rmtree(paths.venv_next, ignore_errors=True)
        self.out.say("→ Building the new environment beside the current one...")
        self.p.built = True
        env = dict(self.req.env)
        env["UV_PROJECT_ENVIRONMENT"] = str(paths.venv_next)
        env.pop("VIRTUAL_ENV", None)
        failed = self._child(
            [uv, "venv", "--relocatable", "--quiet", str(paths.venv_next)],
            env=env,
            timeout=_UV_TIMEOUT_S,
        )
        if not failed:
            sync = [uv, "sync", "--frozen", "--quiet"]
            for extra in self.req.extras:
                sync += ["--extra", extra]
            if not self._keeps_dev_group():
                sync.append("--no-dev")
            failed = self._child(sync, env=env, timeout=_UV_TIMEOUT_S)
        if not failed:
            failed = self._child(
                [str(venv_python(paths.venv_next)), "-m", "indus", "--version"],
                env=self.req.env,
                timeout=_VALIDATE_TIMEOUT_S,
            )
        if failed:
            self.receipt.phase("venv", "failed", failed.splitlines()[-1])
            raise _Abort(
                "The new environment could not be built, so the update was rolled back and the "
                f"current environment kept:\n  {failed.splitlines()[-1]}"
            )
        self.receipt.phase("venv", "ok", "built and checked")

    def swap(self) -> None:
        paths = self.req.paths
        shutil.rmtree(paths.venv_prev, ignore_errors=True)
        self.p.had_venv = paths.venv.exists()
        if self.p.had_venv:
            os.replace(paths.venv, paths.venv_prev)
        try:
            os.replace(paths.venv_next, paths.venv)
        except OSError:
            if self.p.had_venv:
                os.replace(paths.venv_prev, paths.venv)
            raise
        self.p.swapped = True
        self.p.built = False
        self.receipt.phase("swap", "ok", ".venv-next → .venv (previous kept as .venv-prev)")

    def reapply_stash(self) -> str | None:
        """Put local edits back on the new tree. Returns the label when they would not apply."""
        if self.p.stash is None:
            return None
        if self.git.ok("stash", "apply", self.p.stash):
            self._drop_stash()
            self.receipt.phase("restore_changes", "ok", self.p.stash_label)
            return None
        self.git.run("reset", "--quiet", "--hard", "HEAD", check=False)
        label = self.p.stash_label
        self.receipt.phase("restore_changes", "failed", f"kept in the stash as {label}")
        self.p.stash = None
        return label

    def _drop_stash(self) -> None:
        listed = self.git.run("stash", "list", "--format=%H", check=False).stdout.split()
        if self.p.stash in listed:
            self.git.run("stash", "drop", "--quiet", f"stash@{{{listed.index(self.p.stash)}}}")
        self.p.stash = None

    # -- undo --------------------------------------------------------------------------------------

    def undo(self) -> None:
        """Put back what the steps changed, newest first. Never raises."""
        paths = self.req.paths
        with contextlib.suppress(OSError):
            if self.p.swapped:
                shutil.rmtree(paths.venv_next, ignore_errors=True)
                os.replace(paths.venv, paths.venv_next)
                if self.p.had_venv:
                    os.replace(paths.venv_prev, paths.venv)
                self.p.swapped = False
                self.p.built = True
        if self.p.built:
            shutil.rmtree(paths.venv_next, ignore_errors=True)
            self.p.built = False
        if self.p.moved:
            self._restore_code()
        if self.p.stash is not None:
            if self.git.ok("stash", "apply", self.p.stash):
                self._drop_stash()
            else:
                self.out.warn(
                    f"⚠ Your local changes are kept in the stash as {self.p.stash_label} "
                    "(git stash list; git stash apply)."
                )

    def _restore_code(self) -> None:
        git, p = self.git, self.p
        try:
            if p.pre_branch == MAIN and p.pre_main is not None:
                git.run("update-ref", f"refs/heads/{MAIN}", p.pre_main)
            if p.pre_branch is not None:
                git.run("checkout", "--quiet", "--force", p.pre_branch)
            else:
                git.run("checkout", "--quiet", "--force", "--detach", p.pre)
            git.run("reset", "--quiet", "--hard", p.pre)
            if p.pre_branch != MAIN:  # ``main`` was moved (or made) by the update: put it back
                if p.pre_main is None:
                    git.run("update-ref", "-d", f"refs/heads/{MAIN}", check=False)
                else:
                    git.run("update-ref", f"refs/heads/{MAIN}", p.pre_main, check=False)
        except GitError as exc:
            self.out.warn(
                f"✗ Could not put the checkout back by itself ({exc.detail or exc}). "
                f"Recover with: git -C {self.req.paths.checkout} reset --hard {p.pre}"
            )
            self.receipt.phase("rollback", "failed", p.pre)
            return
        p.moved = False
        self.out.say(f"  ✓ Rolled back to {p.pre[:10]}; your install is unchanged.")
        self.receipt.phase("rollback", "ok", p.pre[:10])

    # -- hand-off ----------------------------------------------------------------------------------

    def hand_off(self, stash_conflict: str | None) -> None:
        req, paths = self.req, self.req.paths
        target = req.target
        self.receipt.phase("handoff", "ok", "exec --finish in the new code")
        request: dict[str, Any] = {
            "schema": REQUEST_SCHEMA,
            "checkout": str(paths.checkout),
            "root": str(paths.root),
            "profile_home": str(req.profile_home),
            "channel": req.choice.channel,
            "channel_source": req.choice.source,
            "from_commit": self.p.pre,
            "to_commit": target.commit,
            "to_version": None if target.version is None else str(target.version),
            "assume_yes": req.assume_yes,
            "no_gateway_restart": req.no_gateway_restart,
            "stash_conflict": stash_conflict,
            "receipt": self.receipt.to_json(),
        }
        paths.requests.mkdir(parents=True, exist_ok=True)
        path = atomic_write_json(paths.requests / f"{self.stamp}.json", request)
        python = str(venv_python(paths.venv))
        argv = [python, "-m", "indus", "update", "--finish", str(path), *req.finish_argv]
        self.out.say("→ Finishing on the new code...")
        try:
            self.hooks.execv(python, argv, dict(req.env))
        except OSError as exc:  # the code and venv are in place; only the finish is missing
            retry = f"{python} -m indus update --finish {path}"
            self.receipt.owe("finish", f"could not start the new code: {exc}", retry=retry)
            self.receipt.write(paths.receipts, finished=req.now(), outcome="finish_not_started")
            raise _HandOffFailed(
                f"Could not start the new code ({exc}). Finish with: {retry}"
            ) from exc


def apply_update(
    git: Git,
    request: ApplyRequest,
    hooks: ApplyHooks,
    console: Console,
    receipt: Receipt,
) -> int:
    """Run the steps (see the module notes). Returns an exit code when it does not hand off;
    after a successful hand-off it returns only when ``hooks.execv`` returns (tests)."""
    step = _Applier(git, request, hooks, console, receipt)
    receipt.from_commit = git.head()
    receipt.to_commit = request.target.commit
    receipt.to_version = "" if request.target.version is None else str(request.target.version)
    try:
        step.snapshot()
        step.stash()
        step.move()
        step.syntax()
        step.venv()
        step.swap()
        conflict = step.reapply_stash()
    except KeyboardInterrupt:
        console.warn("✗ Interrupted; putting everything back...")
        step.undo()
        receipt.phase("update", "interrupted")
        receipt.write(request.paths.receipts, finished=request.now(), outcome="interrupted")
        return EXIT_INTERRUPTED
    except (_Abort, GitError, OSError) as exc:
        detail = getattr(exc, "detail", "")
        console.warn(f"✗ {exc}" + (f"\n  {detail}" if detail else ""))
        step.undo()
        receipt.write(request.paths.receipts, finished=request.now(), outcome="failed")
        return 1
    try:
        step.hand_off(conflict)
    except _HandOffFailed as exc:
        console.warn(f"✗ {exc}")
        return 1
    return 0
