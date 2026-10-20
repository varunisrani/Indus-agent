"""``indus acp``: the editor agent over stdio (P19-01).

Order (R09.14, R09.15):

1. The informational flags run first and start nothing: ``--version`` prints the version,
   ``--check`` imports the ACP SDK and the adapter and prints OK (exit 1 with an install hint when
   the ``acp`` extra is missing), ``--setup`` runs ``indus setup`` as a child process and then,
   on a terminal, offers the browser tools, ``--setup-browser [--yes]`` installs them (also a
   child ``indus`` process: this package may not import the CLI).
2. ``stdout_guard.claim()`` takes descriptors 0 and 1 for the protocol and guards ``sys.stdout``
   before anything heavy is imported. Logging is already the process's ``stdio`` mode (stderr,
   redacted, HTTP client loggers at WARNING): the CLI lists ``acp`` among its stdio commands.
3. On Windows the native modules that deadlocked when first imported off the main thread (the
   keyring backend, the memory providers) are imported here, before the reader starts.
4. The SDK's JSON-RPC ``Connection`` runs over asyncio streams on the private descriptors with
   ``AcpAgent.dispatch`` as its handler, until the editor closes its end (or SIGTERM).
5. The backend is closed while its loop is alive. Sessions are never ended here: an attached
   daemon ends an ``acp`` session once no viewer is left and nothing is queued (D-16.421); local
   mode only detaches.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, TextIO

from indus import __version__
from indus.foundation.scope import Scope
from indus.platform.locate import which

__all__ = [
    "MISSING_SDK_HINT",
    "Options",
    "check",
    "indus_command",
    "make_selector",
    "run",
    "serve",
    "setup",
    "setup_browser",
]

log = logging.getLogger(__name__)

MISSING_SDK_HINT: Final = (
    "The Agent Client Protocol SDK is not installed. Install Indus Agent with the `acp` extra, "
    "for example: uv tool install 'indus-agent[acp]'"
)
ADAPTER_MODULES: Final = (
    "indus.acp.agent",
    "indus.acp.sessions",
    "indus.acp.history",
    "indus.acp.listing",
    "indus.acp.local_host",
    "indus.acp.attached_host",
)


@dataclass(frozen=True)
class Options:
    local: bool = False
    version: bool = False
    check: bool = False
    setup: bool = False
    setup_browser: bool = False
    yes: bool = False


def indus_command() -> list[str]:
    """How to start this program again as a child process."""
    found = which("indus")
    if found:
        return [found]
    return [sys.executable, "-m", "indus"]


# -- informational flags --------------------------------------------------------------------------


def check(out: TextIO | None = None, err: TextIO | None = None) -> int:
    try:
        import acp
    except ImportError:
        print(MISSING_SDK_HINT, file=err or sys.stderr)
        return 1
    import importlib

    for name in ADAPTER_MODULES:
        importlib.import_module(name)
    version = getattr(acp, "__version__", None) or _sdk_version()
    print(
        f"Indus Agent ACP check OK (agent-client-protocol {version}, protocol "
        f"{acp.PROTOCOL_VERSION})",
        file=out or sys.stdout,
    )
    return 0


def _sdk_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("agent-client-protocol")
    except PackageNotFoundError:
        return "unknown"


type Runner = Callable[..., subprocess.CompletedProcess[Any]]


def setup(
    *,
    run_child: Runner = subprocess.run,
    stdin: TextIO | None = None,
    ask: Callable[[str], str] = input,
) -> int:
    """``indus setup`` in a child process, then (on a terminal) offer the browser tools."""
    code = run_child([*indus_command(), "setup"], check=False).returncode
    if code != 0:
        return int(code)
    source = stdin or sys.stdin
    if not source.isatty():
        return 0
    try:
        reply = ask("\nInstall the browser tools as well (downloads Chromium)? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return 0
    if reply.strip().lower() in {"y", "yes"}:
        return setup_browser(yes=False, run_child=run_child)
    return 0


def setup_browser(*, yes: bool, run_child: Runner = subprocess.run) -> int:
    argv = [*indus_command(), "tools", "install", "browser"]
    if yes:
        argv.append("--yes")
    code = int(run_child(argv, check=False).returncode)
    if code != 0:
        sys.stderr.write("Browser setup did not finish; see the messages above.\n")
    return code


# -- the server -----------------------------------------------------------------------------------


def _allow_fallback(scope: Scope) -> bool:
    from indus.foundation.config import load

    try:
        return load(scope).get("acp.allow_local_fallback") is True
    except Exception:
        return False


def make_selector(scope: Scope, *, local: bool) -> Any:
    """The host selector for this process (``backend.HostSelector``)."""
    from indus.acp.attached_host import AttachedHost
    from indus.acp.backend import AcpBackend, HostSelector
    from indus.acp.local_host import LocalHost, build_service

    async def attached() -> AcpBackend:
        host = AttachedHost(scope)
        try:
            await host.start()
        except BaseException:
            await host.aclose()
            raise
        return host

    async def in_process() -> AcpBackend:
        return LocalHost(scope, lambda factory: build_service(scope, factory))

    return HostSelector(
        local_only=local,
        allow_fallback=_allow_fallback(scope),
        attached=attached,
        local=in_process,
    )


def _warm_native_imports() -> None:
    """Windows: import native modules on the main thread before any reader thread (R09.15)."""
    if sys.platform != "win32":
        return
    for name in ("keyring", "indus.learning.memory.providers"):
        with contextlib.suppress(Exception):
            __import__(name)
    with contextlib.suppress(Exception):
        import keyring

        keyring.get_keyring()


class SdkPeer:
    """``agent.ClientPeer`` over the SDK connection."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._connection.send_notification(method, dict(params))

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        return await self._connection.send_request(method, dict(params))


async def _serve(scope: Scope, claimed: Any, *, local: bool) -> int:
    from acp.connection import Connection

    from indus.acp.agent import AcpAgent

    reader, writer = await claimed.streams()
    agent = AcpAgent(scope, make_selector(scope, local=local))
    connection = Connection(agent.dispatch, writer, reader, listening=False)
    agent.peer = SdkPeer(connection)
    main = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if main is not None and sys.platform != "win32":
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGTERM, main.cancel)
    try:
        await connection.main_loop()
    except asyncio.CancelledError:
        log.info("the editor agent was asked to stop")
    except Exception:
        log.exception("the editor connection failed")
        return 1
    finally:
        with contextlib.suppress(Exception):
            await agent.aclose()
        with contextlib.suppress(Exception):
            await connection.close()
    return 0


def serve(scope: Scope, *, local: bool) -> int:
    from indus.acp import stdout_guard

    claimed = stdout_guard.claim()
    try:
        import acp  # noqa: F401
    except ImportError:
        sys.stderr.write(MISSING_SDK_HINT + "\n")
        return 1
    _warm_native_imports()
    log.info("starting the editor agent (%s)", "local" if local else "attached")
    try:
        return asyncio.run(_serve(scope, claimed, local=local))
    except KeyboardInterrupt:
        return 0


def run(scope: Scope, options: Options) -> int:
    """``indus acp`` (see the module notes)."""
    if options.version:
        sys.stdout.write(f"Indus Agent {__version__}\n")
        return 0
    if options.check:
        return check()
    if options.setup:
        return setup()
    if options.setup_browser:
        return setup_browser(yes=options.yes)
    return serve(scope, local=options.local)
