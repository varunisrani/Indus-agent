"""Scriptable fake ``git`` and ``curl`` for the installer tests, plus the sandbox that drives them.

Run as ``fake_tools.py git|curl ARGS…`` (through ``tests/fakes/install/bin/{git,curl}``). Every call
is appended to ``$INDUS_FAKE_TOOLS_STATE/calls.jsonl``. Behaviour comes from the scenario file
``$INDUS_FAKE_TOOLS`` (JSON ``{"rules": [...]}``); the first rule whose ``tool`` matches and whose
``match`` strings each appear inside some argument applies:

    {"tool": "git", "match": ["clone"], "times": 2, "exit": 128,
     "stderr": "fatal: early EOF", "partial": true}
    {"tool": "curl", "match": ["api.github.com"], "exit": 22}
    {"tool": "curl", "match": ["/uv-"], "body_file": "/path/archive.tar.gz"}
    {"tool": "curl", "match": ["releases/latest"], "stdout": "location: …/tag/v1.2.0\\n"}

``times`` limits how often a rule applies (counted in the state dir); ``partial`` makes a failing
``git clone`` leave a half-written target directory first, like a dropped connection does.
Unmatched ``git`` calls run the real git (``$INDUS_FAKE_REAL_GIT``); unmatched ``curl`` calls fail
with exit 7 (could not connect) — the tests never reach a real network.

Imported, the module offers `Sandbox` (HOME, fake PATH, scenario, harness runner) and the small
builders the tests share (origin repositories, uv archives, ELF headers).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BIN = HERE / "bin"
ROOT = HERE.parents[2]
INSTALL_SH = ROOT / "scripts" / "install.sh"


# -- the fake tools (script side, standard library only) -----------------------------------------


def _state() -> Path:
    path = Path(os.environ["INDUS_FAKE_TOOLS_STATE"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rules() -> list[dict[str, Any]]:
    raw = os.environ.get("INDUS_FAKE_TOOLS", "")
    if not raw:
        return []
    return json.loads(Path(raw).read_text(encoding="utf-8")).get("rules", [])


def _matches(rule: dict[str, Any], tool: str, argv: list[str]) -> bool:
    if rule.get("tool") != tool:
        return False
    return all(any(want in arg for arg in argv) for want in rule.get("match", []))


def _take(index: int, rule: dict[str, Any]) -> bool:
    """Whether the rule still applies (its ``times`` budget), counting this use."""
    if "times" not in rule:
        return True
    counter = _state() / f"rule-{index}.count"
    used = int(counter.read_text()) if counter.exists() else 0
    if used >= int(rule["times"]):
        return False
    counter.write_text(str(used + 1))
    return True


def _clone_target(argv: list[str]) -> str | None:
    words = [a for a in argv[argv.index("clone") + 1 :] if not a.startswith("-")]
    # --branch NAME: drop the value that follows the option
    if "--branch" in argv:
        name = argv[argv.index("--branch") + 1]
        if name in words:
            words.remove(name)
    return words[-1] if len(words) >= 2 else None


def _apply(rule: dict[str, Any], tool: str, argv: list[str]) -> int:
    if tool == "git" and rule.get("partial") and "clone" in argv:
        target = _clone_target(argv)
        if target:
            Path(target, ".git").mkdir(parents=True, exist_ok=True)
            Path(target, "half-written").write_text("partial\n")
    if tool == "curl" and int(rule.get("exit", 0)) == 0:
        body: bytes
        if "body_file" in rule:
            body = Path(rule["body_file"]).read_bytes()
        else:
            body = str(rule.get("body", "")).encode()
        if "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_bytes(body)
        else:
            sys.stdout.buffer.write(body)
    if "stdout" in rule:
        sys.stdout.write(str(rule["stdout"]))
    if "stderr" in rule:
        sys.stderr.write(str(rule["stderr"]))
    sys.stdout.flush()
    return int(rule.get("exit", 0))


def tool_main(tool: str, argv: list[str]) -> int:
    with (_state() / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"tool": tool, "argv": argv}) + "\n")
    for index, rule in enumerate(_rules()):
        if _matches(rule, tool, argv) and _take(index, rule):
            return _apply(rule, tool, argv)
    if tool == "git":
        real = os.environ["INDUS_FAKE_REAL_GIT"]
        os.execv(real, [real, *argv])
    sys.stderr.write(f"curl: (7) Failed to connect (fake curl: no rule for {argv})\n")
    return 7


# -- test side ------------------------------------------------------------------------------------


def run_git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout.strip()


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Installer Test",
    "GIT_AUTHOR_EMAIL": "installer-test@example.invalid",
    "GIT_COMMITTER_NAME": "Installer Test",
    "GIT_COMMITTER_EMAIL": "installer-test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class Sandbox:
    """A temp HOME and fake tool PATH in which the real ``install.sh`` runs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.indus_home = self.home / ".indus"
        self.state = root / "fake-state"
        self.state.mkdir(exist_ok=True)
        self.scenario_file = root / "scenario.json"
        self.rules: list[dict[str, Any]] = []
        self.save()
        self.extra_bin = root / "extra-bin"
        self.extra_bin.mkdir(exist_ok=True)
        real_git = subprocess.run(
            ["sh", "-c", "command -v git"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.real_git = real_git

    def save(self) -> None:
        self.scenario_file.write_text(json.dumps({"rules": self.rules}), encoding="utf-8")

    def rule(self, **rule: Any) -> None:
        self.rules.append(rule)
        self.save()

    def env(self, **extra: str) -> dict[str, str]:
        base = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("INDUS_", "GIT_", "UV_", "VIRTUAL_ENV", "NO_COLOR"))
            and k not in ("CI", "GITHUB_ACTIONS", "TERMUX_VERSION", "PREFIX", "ZDOTDIR")
        }
        base.update(GIT_IDENTITY)
        base.update(
            {
                "HOME": str(self.home),
                "INDUS_HOME": str(self.indus_home),
                "PATH": f"{self.extra_bin}:{BIN}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "SHELL": "/bin/bash",
                "LC_ALL": "C",
                "INDUS_FAKE_TOOLS": str(self.scenario_file),
                "INDUS_FAKE_TOOLS_STATE": str(self.state),
                "INDUS_FAKE_REAL_GIT": self.real_git,
                "INDUS_FAKE_PYTHON": sys.executable,
            }
        )
        base.update(extra)
        return base

    def git(self, *args: str, cwd: Path | None = None) -> str:
        env = self.env()
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        return run_git(*args, cwd=cwd, env=env)

    def run_git_allow_fail(self, args: list[str], cwd: Path) -> int:
        env = self.env()
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, timeout=60, check=False
        ).returncode

    def run_bash(self, script: str) -> str:
        """Run a bash snippet in the sandbox; its stdout, stripped."""
        result = subprocess.run(
            ["bash", "-c", script],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            start_new_session=True,
        )
        return result.stdout.strip()

    def harness(self, prelude: str = "") -> Path:
        """A script that sources the built installer, applies ``prelude`` and runs main."""
        path = self.root / "harness.sh"
        path.write_text(
            f'source "{INSTALL_SH}"\n'
            'ins_sleep() { printf \'%s\\n\' "$1" >>"$INDUS_FAKE_TOOLS_STATE/sleeps"; }\n'
            f"{prelude}\n"
            'ins_main "$@"\n',
            encoding="utf-8",
        )
        return path

    def run(
        self,
        *args: str,
        prelude: str = "",
        env: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.harness(prelude)), *args],
            env=env or self.env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
            cwd=self.root,
        )

    def calls(self, tool: str) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [row["argv"] for row in rows if row["tool"] == tool]

    def sleeps(self) -> list[str]:
        path = self.state / "sleeps"
        return path.read_text().split() if path.exists() else []


OFFICIAL_URL = "https://github.com/indusagi/indus-agent.git"


def make_origin(sandbox: Sandbox, tags: tuple[str, ...] = ("v0.1.0",)) -> Path:
    """A bare origin with a few commits on main; each tag marks one commit, in order.

    The sandbox's git config maps our official URL onto it (``insteadOf``), so the installer
    clones "https://github.com/…" while git reads the local repository."""
    work = sandbox.root / "origin-work"
    bare = sandbox.root / "origin.git"
    sandbox.git("init", "-q", "-b", "main", str(work))
    (work / "uv.lock").write_text("version = 1\n")
    (work / "README.md").write_text("base\n")
    sandbox.git("add", ".", cwd=work)
    sandbox.git("commit", "-q", "-m", "base", cwd=work)
    for index, tag in enumerate(tags):
        (work / "README.md").write_text(f"release {index}\n")
        sandbox.git("commit", "-q", "-am", f"release {tag}", cwd=work)
        sandbox.git("tag", tag, cwd=work)
    (work / "README.md").write_text("tip of main\n")
    sandbox.git("commit", "-q", "-am", "after the releases", cwd=work)
    sandbox.git("clone", "-q", "--bare", str(work), str(bare))
    sandbox.git("config", "--global", f"url.{bare}.insteadOf", OFFICIAL_URL)
    sandbox.git("config", "--global", "uploadpack.allowFilter", "true")
    sandbox.git("config", "--global", "protocol.file.allow", "always")
    return bare


def push_commit(sandbox: Sandbox, bare: Path, message: str, *, tag: str | None = None) -> str:
    """Add a commit on the origin's main (and optionally tag it); returns its id."""
    work = sandbox.root / "origin-work"
    (work / "README.md").write_text(message + "\n")
    sandbox.git("commit", "-q", "-am", message, cwd=work)
    if tag:
        sandbox.git("tag", tag, cwd=work)
    sandbox.git("push", "-q", "--tags", str(bare), "main", cwd=work)
    return sandbox.git("rev-parse", "HEAD", cwd=work)


def elf_header(interpreter: str) -> bytes:
    """The first bytes of a dynamically linked ELF binary naming ``interpreter`` (a few hundred
    bytes: magic, zero padding, the PT_INTERP string)."""
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 57 + b"\x00" * 128 + interpreter.encode() + b"\x00"


FAKE_UV = """#!/bin/sh
# A fake uv for the installer tests: logs its arguments, answers what the installer asks.
printf '%s\\n' "$*" >>"$INDUS_FAKE_TOOLS_STATE/uv-calls"
case "$1" in
    --version) echo "uv 0.0.0-fake" ;;
    python)
        case "$2" in
            find)
                installed="$INDUS_FAKE_TOOLS_STATE/python-installed"
                if [ -n "${FAKE_UV_NEED_INSTALL:-}" ] && [ ! -e "$installed" ]; then exit 2; fi
                [ -n "${FAKE_UV_PYTHON:-}" ] && echo "$FAKE_UV_PYTHON" || exit 2
                ;;
            install)
                : >"$INDUS_FAKE_TOOLS_STATE/python-installed"
                exit "${FAKE_UV_INSTALL_EXIT:-0}"
                ;;
        esac
        ;;
    sync)
        dir=""
        while [ $# -gt 0 ]; do
            if [ "$1" = --directory ]; then dir="$2"; fi
            shift
        done
        if [ -n "${FAKE_UV_VENV:-}" ]; then
            ln -sfn "$FAKE_UV_VENV" "$dir/.venv"
        else
            mkdir -p "$dir/.venv/bin"
            printf '#!/bin/sh\\necho "Indus Agent 0.0.0"\\n' >"$dir/.venv/bin/indus"
            chmod 755 "$dir/.venv/bin/indus"
        fi
        ;;
esac
"""


def uv_archive(path: Path, script: str = FAKE_UV, *, name: str = "uv") -> bytes:
    """A uv release-shaped archive (``uv-<triple>/uv`` and ``uvx``) holding ``script``; returns
    the archive bytes (also written to ``path``)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for member in (name, "uvx"):
            data = script.encode()
            info = tarfile.TarInfo(f"uv-x86_64-unknown-linux-gnu/{member}")
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


if __name__ == "__main__":
    sys.exit(tool_main(sys.argv[1], sys.argv[2:]))
