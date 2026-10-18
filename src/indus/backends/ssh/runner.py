# pyright: strict
"""Short client calls of the SSH backend: probes, uploads, ``-O exit`` (P11-09, P11-10).

``Runner`` is the seam tests replace. The shipped ``run_client`` runs the argv on the blocking
``io`` pool with ``subprocess.run``: stdin from a file or closed (a client that unexpectedly
prompts can never wait on us), stdout captured as text or written to a file, a hard timeout that
kills the client and raises ``TimeoutError``.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Protocol

from indus.foundation.blocking import run_blocking

__all__ = ["RunResult", "Runner", "run_client"]


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    async def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        stdin_path: str | None = None,
        stdout_path: str | None = None,
    ) -> RunResult:
        """Run ``argv`` to the end; ``TimeoutError`` when it takes longer than ``timeout``."""
        ...


def _run(
    argv: Sequence[str],
    env: Mapping[str, str],
    timeout: float,
    stdin_path: str | None,
    stdout_path: str | None,
) -> RunResult:
    with contextlib.ExitStack() as stack:
        stdin: IO[bytes] | int = subprocess.DEVNULL
        if stdin_path is not None:
            stdin = stack.enter_context(open(stdin_path, "rb"))  # noqa: SIM115
        stdout: IO[bytes] | int = subprocess.PIPE
        if stdout_path is not None:
            stdout = stack.enter_context(open(stdout_path, "wb"))  # noqa: SIM115
        try:
            done = subprocess.run(  # noqa: S603 - argv is built by the backend
                list(argv),
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                env=dict(env),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"{argv[0]} did not finish in {timeout:g}s") from exc
    raw_out: bytes | None = done.stdout
    raw_err: bytes | None = done.stderr
    out = raw_out.decode("utf-8", "replace") if raw_out else ""
    err = raw_err.decode("utf-8", "replace") if raw_err else ""
    return RunResult(done.returncode, out, err)


async def run_client(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    stdin_path: str | None = None,
    stdout_path: str | None = None,
) -> RunResult:
    return await run_blocking(
        _run, argv, env, timeout, stdin_path, stdout_path, pool="io", label="ssh client"
    )
