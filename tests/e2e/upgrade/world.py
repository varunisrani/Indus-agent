"""The temp world the release-lane end-to-end tests share (P25-22, P25-23).

``Rehearsal`` holds a bare repository that plays ``origin``, releases built from this checkout's
own source tree, the user's HOME, the Indus home and a source install made with the real
``install.sh`` stages. Only the dependencies step is done here: the installer's own runtime and
dependencies stages download a pinned uv and sync the standard extras, so the test runs the same
``uv sync --frozen --no-dev`` offline from the uv cache the lane's own ``uv sync`` filled. Every
child gets ``UV_OFFLINE=1`` and the running interpreter as ``UV_PYTHON``: nothing reaches the
network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.fixtures.update.make_repo import Upstream

ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = ROOT / "scripts" / "install.sh"
MODEL_SERVER = ROOT / "scripts" / "parity" / "scripted_model_server.py"
# Read at import: the root conftest drops every INDUS_* variable before each test runs.
IN_RELEASE_LANE = os.environ.get("INDUS_RELEASE_LANE") == "1"
UV = shutil.which("uv")
STEP_TIMEOUT_S = 300.0
TURN_TIMEOUT_S = 120.0

# What a source install needs from the repository (the build backend reads the readme, licence,
# skills, locales and the build hook; the update reads the lock and the store schema; the smoke
# checks the notices).
RELEASE_PATHS = (
    ".gitignore",
    ".python-version",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "locales",
    "pyproject.toml",
    "scripts/build_hooks",
    "scripts/parity/scripted_model_server.py",
    "skills",
    "src",
    "uv.lock",
)

RELEASE_LANE_MARKS = [
    pytest.mark.release,
    pytest.mark.slow,
    pytest.mark.platforms("posix"),
    pytest.mark.skipif(not IN_RELEASE_LANE, reason="release lane only (INDUS_RELEASE_LANE=1)"),
    pytest.mark.skipif(UV is None, reason="needs uv on PATH"),
]


def source_files() -> list[str]:
    """Tracked (and new, not ignored) files of this checkout that a release carries."""
    done = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *RELEASE_PATHS,
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return sorted({name for name in done.stdout.decode("utf-8").split("\0") if name})


def uv_cache_dir() -> str:
    """The cache the lane's own ``uv sync`` filled (the children get another HOME)."""
    configured = os.environ.get("UV_CACHE_DIR")
    if configured:
        return configured
    assert UV is not None
    done = subprocess.run([UV, "cache", "dir"], capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


def base_python() -> str:
    """The interpreter behind the running one (a venv's ``python`` names its base)."""
    return str(getattr(sys, "_base_executable", "") or sys.executable)


@dataclass
class Rehearsal:
    """One temp world: ``origin``, the user's HOME, the Indus home and the install.

    The default layout keeps HOME, the Indus home and the checkout side by side; ``smoke=True``
    uses the installers' default layout under HOME (``~/.indus``, ``~/.indus/indus-agent``), the
    one the smoke scripts expect."""

    base: Path
    files: Sequence[str]
    smoke: bool = False
    transcript: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        self.upstream = Upstream(self.base)
        if self.smoke:
            self.user = self.base / "home"
            self.home = self.user / ".indus"
            self.checkout = self.home / "indus-agent"
        else:
            self.user = self.base / "user"
            self.home = self.base / "indus-home"
            self.checkout = self.base / "indus-agent"
        self.work = self.base / "work"
        for folder in (self.user, self.home, self.work):
            folder.mkdir(parents=True, exist_ok=True)
        self.launcher = self.user / ".local" / "bin" / "indus"
        self.cache = uv_cache_dir()

    # -- the releases ------------------------------------------------------------------------------

    def release(self, version: str) -> str:
        """Commit this checkout's source tree as ``version`` and tag it ``v<version>``."""
        work = self.upstream.work
        for name in self.files:
            source = ROOT / name
            if not source.is_file():
                continue  # deleted in the working tree
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (work / "RELEASE.txt").write_text(
            f"Indus Agent {version} (built locally for the release rehearsal)\n", encoding="utf-8"
        )
        sha = self.upstream.commit(f"release {version}")
        self.upstream.tag(f"v{version}")
        return sha

    # -- children ----------------------------------------------------------------------------------

    def env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.user),
            "INDUS_HOME": str(self.home),
            "INDUS_TERMINAL_ID": "release-rehearsal",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "UV_CACHE_DIR": self.cache,
            "UV_OFFLINE": "1",
            "UV_NO_CONFIG": "1",
            "UV_PYTHON": base_python(),
            "UV_PYTHON_DOWNLOADS": "never",
        }

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = STEP_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            [str(a) for a in argv],
            cwd=self.work,
            env={**self.env(), **(env or {})},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        command = " ".join(str(a) for a in argv)
        self.transcript.append(f"$ {command}\n[exit {done.returncode}]\n{done.stdout}{done.stderr}")
        return done

    def ok(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> str:
        done = self.run(argv, env=env)
        assert done.returncode == 0, self.story()
        return done.stdout

    def indus(
        self, *args: str, timeout: float = STEP_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        """The installed ``indus`` through the launcher the installer wrote."""
        return self.run([str(self.launcher), *args], timeout=timeout)

    def story(self) -> str:
        return "\n".join(self.transcript[-12:])

    # -- the install -------------------------------------------------------------------------------

    def stage(self, name: str, channel: str) -> None:
        self.ok(
            [
                "bash", str(INSTALL_SH), "--stage", name, "--channel", channel,
                "--dir", str(self.checkout), "--indus-home", str(self.home),
                "--non-interactive", "--skip-browser", "--skip-computer-use",
            ],
            env={"INDUS_INSTALL_REPO_URL": str(self.upstream.origin)},
        )  # fmt: skip

    def install(self, channel: str) -> str:
        """The installer's stages, the dependencies step done offline; returns the install id."""
        self.stage("prerequisites", channel)
        self.stage("repository", channel)
        assert UV is not None
        self.ok([UV, "sync", "--directory", str(self.checkout), "--frozen", "--no-dev", "--quiet"])
        for name in ("config", "products", "complete"):
            self.stage(name, channel)
        iid = self.ok([str(self.launcher), "update", "--install-id"]).split()[0]
        # The cache holds the base packages only: keep later syncs to that set.
        self.ok(
            [str(self.launcher), "config", "set", "--force", f"updates.installs.{iid}.extras", "[]"]
        )
        return iid

    def gateway_pid(self) -> int | None:
        """The pid a gateway daemon of this home recorded, when that process still runs."""
        try:
            pid = int(json.loads((self.home / "gateway.pid").read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid


@dataclass
class ModelServer:
    """The scripted model server (P25-06), our own child; stopped by closing its stdin."""

    process: subprocess.Popen[str]
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


@contextmanager
def running_model_server() -> Iterator[ModelServer]:
    process = subprocess.Popen(
        [sys.executable, str(MODEL_SERVER), "--port", "0", "--default-script", "echo"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        port = int(json.loads(process.stdout.readline())["port"])
        yield ModelServer(process, port)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
