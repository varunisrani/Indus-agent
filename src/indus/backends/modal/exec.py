# pyright: strict
"""Modal direct mode: running commands and moving files (P21-06).

Commands use the shared SDK shell flow (``sdk_shell``): ``Sandbox.exec("bash", "-c", <guarded
script>, text=False)``, stdout streamed into the wait loop while the command runs (the script
folds stderr into it), stdin streamed in chunks and closed (``pipe`` mode; without stdin the
stream is closed at once). Interrupt and timeout signal the command's process group through a
second exec; the results are rc 130 + ``interrupted`` and rc 124 + ``timed_out`` as everywhere.
The init snapshot gets ``SNAPSHOT_TIMEOUT_S`` (Modal starts slowly).

Files (``ModalTransport``, the P11 ``SyncTransport``), all over exec stdin/stdout:

- one file: ``mkdir -p`` + ``base64 -d > dest`` with the base64 text streamed on stdin, then the
  written size is printed and compared with the local size (``FILE_TIMEOUT_S``);
- many files: the tar on stdin into ``tar -xf - -C <base>`` (``BULK_TIMEOUT_S``);
- download: ``tar cf - <dir>`` read in full into the local file (``BULK_TIMEOUT_S``, R02.2);
- delete: ``rm -f`` (``DELETE_TIMEOUT_S``).
"""

from __future__ import annotations

import base64
import contextlib
import logging
import math
import os
import posixpath
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import (
    BackendError,
    BackendFlags,
    BackendUnavailable,
    EnvironmentConnectionError,
)
from indus.backends.modal import load_sdk
from indus.backends.modal.lifecycle import (
    ModalSandbox,
    ModalSettings,
    credentials,
    requirement_reason,
    settings_from,
)
from indus.backends.remote_sync import SyncTransport
from indus.backends.sdk_shell import (
    BULK_TIMEOUT_S,
    DELETE_TIMEOUT_S,
    FILE_TIMEOUT_S,
    OP_BULK,
    OP_DELETE,
    OP_FILE,
    SandboxRecords,
    SdkEnvironment,
    SdkProcess,
    SdkSupport,
    config_value,
)
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope

__all__ = [
    "MODAL_FLAGS",
    "SNAPSHOT_TIMEOUT_S",
    "STDIN_CHUNK",
    "ModalEnvironment",
    "ModalTransport",
    "open_modal",
]

log = logging.getLogger(__name__)

MODAL_FLAGS = BackendFlags(isolated=True, container_paths=True, stdin_mode="pipe", host_local=False)
SNAPSHOT_TIMEOUT_S = 60.0
STDIN_CHUNK = 64 * 1024
REMOTE_TEMP = "/tmp"


def _file_chunks(path: str) -> list[bytes]:
    chunks: list[bytes] = []
    with open(path, "rb") as handle:
        while chunk := handle.read(STDIN_CHUNK):
            chunks.append(chunk)
    return chunks


class ModalEnvironment(SdkEnvironment):
    """Commands in a Modal sandbox (see the module notes)."""

    backend_name = "modal"
    default_home = "/root"

    def __init__(
        self,
        scope: Scope,
        support: SdkSupport,
        lifecycle: ModalSandbox,
        settings: ModalSettings,
        *,
        session_id: str | None = None,
        clock: Clock | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        temp_dir: str = REMOTE_TEMP,
        sync_files: bool = True,
    ) -> None:
        super().__init__(
            scope,
            support,
            cwd=settings.cwd,
            timeout=settings.timeout,
            temp_dir=temp_dir,
            flags=MODAL_FLAGS,
            session_id=session_id,
            clock=clock,
            max_chars=max_chars,
            snapshot_timeout=SNAPSHOT_TIMEOUT_S,
            persist_env=bool(config_value(scope, "terminal.persistent_shell")),
            sync_files=sync_files,
        )
        self.lifecycle = lifecycle
        self.settings = settings
        if lifecycle.recreated:
            self.mark_recreated()

    def describe(self) -> str:
        return f"modal sandbox ({self.settings.image}), cwd {self._cwd}"

    def _vendor_exec(
        self, argv: list[str], proc: SdkProcess, stdin_chunks: list[bytes] | None, timeout: float
    ) -> int:
        sandbox = self.lifecycle.sandbox
        if sandbox is None:
            raise BackendError("the modal sandbox is gone")
        extra: dict[str, Any] = {"env": dict(proc.env)} if proc.env else {}
        process = sandbox.exec(*argv, text=False, timeout=max(1, math.ceil(timeout)), **extra)
        proc.vendor = process
        try:
            for chunk in stdin_chunks or ():
                process.stdin.write(chunk)
                process.stdin.drain()
            process.stdin.write_eof()
            process.stdin.drain()
        except Exception as exc:
            if stdin_chunks:
                proc.stdin_error = f"{type(exc).__name__}: {exc}"
        for chunk in process.stdout:
            proc.feed(chunk)
        return int(process.wait())

    def transport(self) -> SyncTransport:
        return ModalTransport(self)

    async def _teardown(self) -> None:
        await self.lifecycle.close()


class ModalTransport:
    """``SyncTransport`` over Modal exec stdin/stdout (see the module notes)."""

    def __init__(self, env: ModalEnvironment) -> None:
        self.env = env

    async def _run(
        self,
        script: str,
        timeout: float,
        op: str,
        what: str,
        *,
        stdin_chunks: list[bytes] | None = None,
        binary: Any = None,
    ) -> str:
        run = await self.env.run_simple(
            script, timeout, op=op, stdin_chunks=stdin_chunks, binary=binary
        )
        if run.returncode != 0:
            detail = run.output.strip().splitlines()
            raise BackendError(
                f"{what} in the modal sandbox failed (exit {run.returncode})"
                + (f": {detail[-1]}" if detail else "")
            )
        return run.output

    async def upload_one(self, path: str, dest: str) -> None:
        with open(path, "rb") as handle:
            data = handle.read()
        encoded = base64.b64encode(data)
        chunks = [encoded[i : i + STDIN_CHUNK] for i in range(0, len(encoded), STDIN_CHUNK)]
        parent = shlex.quote(posixpath.dirname(dest) or "/")
        q = shlex.quote(dest)
        output = await self._run(
            f"mkdir -p -- {parent} && base64 -d > {q} && wc -c < {q}",
            FILE_TIMEOUT_S,
            OP_FILE,
            "copying a file",
            stdin_chunks=chunks,
        )
        words = output.split()
        written = int(words[-1]) if words and words[-1].isdigit() else -1
        if written != len(data):
            raise BackendError(f"copied {written} of {len(data)} bytes to {dest} in the sandbox")

    async def upload_bulk(self, tar_path: str) -> None:
        base = shlex.quote(self.env.remote_base)
        await self._run(
            f"mkdir -p -- {base} && tar -xf - -C {base}",
            BULK_TIMEOUT_S,
            OP_BULK,
            "unpacking synced files",
            stdin_chunks=_file_chunks(tar_path),
        )

    async def download_bulk(self, src: str, dest: str) -> None:
        rel = shlex.quote(src.strip("/") or ".")
        with open(dest, "wb") as out:
            await self._run(
                f"tar cf - --exclude='*.sock' -C / {rel}",
                BULK_TIMEOUT_S,
                OP_BULK,
                "packing remote files",
                binary=out.write,
            )

    async def delete(self, paths: Sequence[str]) -> None:
        if not paths:
            return
        quoted = " ".join(shlex.quote(p) for p in paths)
        await self._run(f"rm -f -- {quoted}", DELETE_TIMEOUT_S, OP_DELETE, "removing files")


async def open_modal(
    scope: Scope,
    *,
    support: SdkSupport,
    records: SandboxRecords,
    sdk: Any = None,
    session_id: str | None = None,
    task_key: str = "default",
    overrides: Mapping[str, Any] | None = None,
    clock: Clock | None = None,
    temp_dir: str = REMOTE_TEMP,
    sync_files: bool = True,
) -> ModalEnvironment:
    """A ready Modal environment for ``scope`` (``BackendUnavailable`` without the extra, the
    token pair or with ``managed``; ``EnvironmentConnectionError`` when Modal cannot be
    reached)."""
    problem = requirement_reason(scope)
    if problem:
        raise BackendUnavailable(problem)
    settings = settings_from(scope, task_key=task_key, overrides=overrides)
    pair = credentials(scope)
    assert pair is not None  # requirement_reason checked it
    module = sdk if sdk is not None else load_sdk()
    lifecycle = ModalSandbox(module, support, records, settings)
    try:
        await lifecycle.open(*pair)
    except (BackendUnavailable, EnvironmentConnectionError):
        await lifecycle.close()
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            await lifecycle.close()
        raise EnvironmentConnectionError(
            f"creating the modal sandbox failed: {type(exc).__name__}",
            retry_hint="Check your Modal token pair and Modal's status, then run it again.",
        ) from exc
    env = ModalEnvironment(
        scope,
        support,
        lifecycle,
        settings,
        session_id=session_id,
        clock=clock,
        max_chars=int(config_value(scope, "tool_output.max_chars")),
        temp_dir=temp_dir,
        sync_files=sync_files,
    )
    try:
        await env.connect()
    except BaseException:
        with contextlib.suppress(Exception):
            await env.cleanup()
        raise
    log.info("modal environment ready (%s)", os.path.basename(settings.image) or settings.image)
    return env
