"""Live lane of the cloud and container backends (P21-16): Modal, Daytona, Vercel Sandbox and
Apptainer/Singularity against the real thing.

Marker ``network``. Each vendor runs only when asked (``INDUS_LIVE_MODAL=1``,
``INDUS_LIVE_DAYTONA=1``, ``INDUS_LIVE_VERCEL=1``, ``INDUS_LIVE_APPTAINER=1``: these lanes create
billable sandboxes, so a stray token alone never starts one), with the vendor's credentials in the
environment (the names ``vendor_env`` declares), the SDK extra installed or the CLI on PATH; the
skip reason names what is missing. All of it is read at import, before the test fixtures drop
inherited ``INDUS_*`` variables.

Each run: ``echo``, write a file, read it back, end the sandbox (persistent: snapshot, stop or
overlay), open it again and read the file once more. The sandbox is keyed by a fresh task id
(``live-<hex>``), so it never meets a profile's own sandbox. What is left afterwards: the last
snapshot (Modal, Vercel), the stopped sandbox (Daytona) or the overlay under the test's temp dir.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import ExecResult
from indus.backends.registry import BackendStatus, EnvironmentRegistry, default_entries
from indus.backends.sdk_entries import extra_missing
from indus.backends.vendor_env import REQUIRED_SETS, VENDOR_TOKEN_NAMES
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig
from tests.fakes.terminal import FakeSecrets

pytestmark = pytest.mark.network

OPT_IN = {
    "modal": os.environ.get("INDUS_LIVE_MODAL") == "1",
    "daytona": os.environ.get("INDUS_LIVE_DAYTONA") == "1",
    "vercel_sandbox": os.environ.get("INDUS_LIVE_VERCEL") == "1",
    "singularity": os.environ.get("INDUS_LIVE_APPTAINER") == "1",
}
OPT_IN_NAME = {
    "modal": "INDUS_LIVE_MODAL",
    "daytona": "INDUS_LIVE_DAYTONA",
    "vercel_sandbox": "INDUS_LIVE_VERCEL",
    "singularity": "INDUS_LIVE_APPTAINER",
}
APPTAINER_IMAGE = os.environ.get("INDUS_LIVE_APPTAINER_IMAGE") or "docker://debian:bookworm-slim"
STEP_TIMEOUT_S = 300.0


def _credentials(backend: str) -> dict[str, str]:
    names = {*VENDOR_TOKEN_NAMES.get(backend, ())}
    names.update(n for group in REQUIRED_SETS.get(backend, ()) for n in group)
    return {name: value for name in sorted(names) if (value := os.environ.get(name))}


CREDENTIALS = {name: _credentials(name) for name in ("modal", "daytona", "vercel_sandbox")}


def _problem(backend: str) -> str | None:
    """Why ``backend``'s live lane cannot run here, or None."""
    if not OPT_IN[backend]:
        return f"set {OPT_IN_NAME[backend]}=1 to run the live {backend} lane"
    if backend == "singularity":
        if shutil.which("apptainer") is None and shutil.which("singularity") is None:
            return "Apptainer or Singularity is not on PATH"
        return None
    missing = extra_missing(backend)
    if missing is not None:
        return missing
    found = CREDENTIALS[backend]
    if not any(all(name in found for name in group) for group in REQUIRED_SETS[backend]):
        wanted = " and ".join(REQUIRED_SETS[backend][0])
        return f"the live {backend} lane needs {wanted} in the environment"
    return None


def _live_scope(scope: Scope, backend: str, tmp_path: Path) -> Scope:
    terminal: dict[str, Any] = {"backend": backend, "container_persistent": True, "timeout": 120}
    if backend == "singularity":
        terminal.update(singularity_image=APPTAINER_IMAGE, scratch_dir=str(tmp_path / "scratch"))
    live = scope.with_config(DictConfig({"terminal": terminal}))
    return live.with_secrets(FakeSecrets(CREDENTIALS.get(backend, {})))


async def _run(registry: EnvironmentRegistry, command: str, **options: Any) -> ExecResult:
    result = await registry.run(command, timeout=STEP_TIMEOUT_S, **options)
    assert not isinstance(result, BackendStatus), result.to_dict()
    assert result.returncode == 0, result.output
    return result


def _round_trip(scope: Scope, backend: str) -> tuple[str, str, str, str]:
    token = os.urandom(6).hex()
    options: dict[str, Any] = {
        "scope": scope,
        "task_id": f"live-{os.urandom(4).hex()}",
        "overrides": {"backend": backend},
    }

    async def go() -> tuple[str, str, str, str]:
        first = EnvironmentRegistry(default_entries())
        try:
            echo = await _run(first, "echo live-ok", **options)
            write = f"printf %s {token} > indus-live.txt && cat indus-live.txt"
            wrote = await _run(first, write, **options)
        finally:
            await first.shutdown(fence=False)  # persistent: snapshot / stop / keep the overlay
        second = EnvironmentRegistry(default_entries())
        try:
            back = await _run(second, "cat indus-live.txt", **options)
        finally:
            await second.shutdown(fence=False)
        return token, echo.output, wrote.output, back.output

    return asyncio.run(go())


@pytest.mark.parametrize("backend", ["modal", "daytona", "vercel_sandbox", "singularity"])
def test_echo_write_read_and_persist_across_recreate(
    scope: Scope, tmp_path: Path, backend: str
) -> None:
    problem = _problem(backend)
    if problem is not None:
        pytest.skip(problem)
    token, echo, wrote, back = _round_trip(_live_scope(scope, backend, tmp_path), backend)
    assert echo.strip() == "live-ok"
    assert wrote == token
    assert back == token  # the file survived the sandbox ending and coming back


def test_the_lane_skips_without_opt_in_or_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(OPT_IN, "modal", False)
    assert "INDUS_LIVE_MODAL=1" in (_problem("modal") or "")
    monkeypatch.setitem(OPT_IN, "daytona", True)
    monkeypatch.setitem(CREDENTIALS, "daytona", {})
    reason = _problem("daytona") or ""
    assert "DAYTONA_API_KEY" in reason or "`daytona` extra" in reason
