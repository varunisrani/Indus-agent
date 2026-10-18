"""Shared pieces of the live backend lanes (P11-21): real clients, real processes, real time.

The lanes skip unless the tools they need are here. They never pull an image on their own: the
Docker lane uses an image already present (``INDUS_LIVE_DOCKER_IMAGE``, default
``debian:bookworm-slim``); the SSH lane builds ``tests/fixtures/backends/sshd`` when
``INDUS_LIVE_SSH=1`` asks for it. Both variables are read at import, before the test fixtures
drop every inherited ``INDUS_*`` variable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from indus.backends.processes import ProcessRecord
from indus.backends.remote_processes import RemoteJob
from indus.foundation.clock import SystemClock

LIVE_IMAGE = os.environ.get("INDUS_LIVE_DOCKER_IMAGE") or "debian:bookworm-slim"
LIVE_SSH = os.environ.get("INDUS_LIVE_SSH") == "1"
REAL = SystemClock()


def docker(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("docker") or "docker"
    return subprocess.run(  # noqa: S603 - fixed client argv
        [binary, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def docker_problem(image: str | None = None) -> str | None:
    """Why the Docker lane cannot run here, or None."""
    if shutil.which("docker") is None:
        return "docker is not installed"
    try:
        if docker("version", timeout=10).returncode != 0:
            return "the Docker daemon is not reachable"
        if image and docker("image", "inspect", image, timeout=10).returncode != 0:
            return f"image {image} is not present (the lanes never pull)"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"docker failed: {exc}"
    return None


def cmdlines() -> list[str]:
    """Every readable ``/proc/<pid>/cmdline`` on this machine."""
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            found.append(
                (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            )
        except OSError:
            continue
    return found


async def poll_until_exit(record: ProcessRecord, seconds: float = 30.0) -> None:
    handle = record.handle
    assert isinstance(handle, RemoteJob) and handle.poller is not None
    deadline = REAL.monotonic() + seconds
    while not record.exited and REAL.monotonic() < deadline:
        await handle.poller.round()
        await REAL.sleep(0.5)
