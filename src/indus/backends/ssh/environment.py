# pyright: strict
"""The SSH backend: commands run on a remote host through the OpenSSH client (P11-09).

Opening: a connect probe (``echo``, ``PROBE_TIMEOUT_S``), remote home detection (``echo $HOME``,
``HOME_TIMEOUT_S``), the remote ``~/.indus/{skills,credentials,cache}`` dirs (``DIRS_TIMEOUT_S``),
the first file push (P11-10) and the shell snapshot (P05 contract). Commands start in ``~`` unless
``terminal.cwd`` names a remote dir. Every command is a fresh ``ssh … bash -c <wrapper>`` with
stdin piped, over a multiplexed connection (P11-08); passthrough variables travel as ``SendEnv``
names with their values in the client's environment only (R02.7), and are kept out of the remote
snapshot so one session's value is never frozen there. When the home could not be detected a
guess is used and detection is tried again before a later command, never sooner than
``HOME_RETRY_S`` after the last failure. Files are pushed before commands (rate-limited by the
sync); nothing is ever pulled back.

Failures of the connection itself — ssh exiting 255 after a refused, timed-out, unresolvable or
dropped connection, a changed host key or failed authentication — raise
``EnvironmentConnectionError``: the registry answers ``degraded`` with the hint and builds a fresh
environment next time. A changed host key is never accepted. Authentication failures name the key
file or the agent, never key material. The remote host is not isolated, so approval prompts stay
on. Cleanup closes every master this environment used (``-O exit``, ``EXIT_TIMEOUT_S`` each).

Stopping a command (interrupt, timeout): killing the local ssh client alone would leave the remote
command running, so every script first writes its remote shell's pid to a file in the remote temp
dir, and a stop sends TERM to that process group over the connection (``REMOTE_KILL_TIMEOUT_S``),
then the P05 ordered kill of the local client, then KILL to what is left remotely. A link that is
down gets no remote signal; the remote command may then keep running until it ends on its own.

Sudo: this machine's sudo password is never sent to the remote host. The P05 rewrite runs here
only when the profile stores ``ssh_sudo_password`` (``sudo.backend_sudo``), never prompts, and its
cache is keyed by ``ssh:user@host:port``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import posixpath
import secrets
import shlex
import subprocess
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import (
    ActivityCallback,
    BackendFlags,
    BackendUnavailable,
    CancelSignal,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.credential_files import resolve as resolve_credentials
from indus.backends.local import SURVIVOR_WINDOW_S, LocalProcess, force_kill_tree, stop_tree
from indus.backends.remote_sync import FileSync, SshTransport, SyncItem, collect_items
from indus.backends.spawned import SNAPSHOT_TIMEOUT_S, SpawnedEnvironment
from indus.backends.ssh.client import (
    SshTarget,
    control_dir,
    control_path,
    exit_argv,
    multiplex_supported,
    send_env_names,
    socket_dir_usable,
    ssh_argv,
)
from indus.backends.ssh.runner import Runner, run_client
from indus.backends.sudo import backend_sudo
from indus.backends.wait import ProcessHandle
from indus.foundation.clock import Clock
from indus.foundation.config.schema import default_for
from indus.foundation.layout import socket_dir
from indus.foundation.scope import Scope
from indus.platform import locate
from indus.safety.child_env import child_env, passthrough_names
from indus.safety.redaction import redact_output

__all__ = [
    "DIRS_TIMEOUT_S",
    "EXIT_TIMEOUT_S",
    "HOME_RETRY_S",
    "HOME_TIMEOUT_S",
    "PROBE_TIMEOUT_S",
    "REMOTE_APP_DIR",
    "REMOTE_KILL_TIMEOUT_S",
    "SSH_FLAGS",
    "SshEnvironment",
    "SshSettings",
    "connection_problem",
    "open_ssh",
    "settings_from",
    "setup_findings",
]

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 15.0
HOME_TIMEOUT_S = 10.0
HOME_RETRY_S = 30.0
DIRS_TIMEOUT_S = 10.0
EXIT_TIMEOUT_S = 5.0
REMOTE_KILL_TIMEOUT_S = 5.0
SSH_LOST_RC = 255
REMOTE_APP_DIR = ".indus"
REMOTE_SUBDIRS = ("skills", "credentials", "cache")
PROBE_TEXT = "indus-ssh-ok"

SSH_FLAGS = BackendFlags(isolated=False, container_paths=True, stdin_mode="pipe", host_local=False)

_HOST_KEY_SIGNS = ("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed")
_AUTH_SIGNS = ("Permission denied (", "Too many authentication failures", "No more authentication")
_LINK_SIGNS = (
    "Connection refused",
    "Connection timed out",
    "Operation timed out",
    "No route to host",
    "Network is unreachable",
    "Could not resolve hostname",
    "Name or service not known",
    "Connection closed by",
    "Connection reset",
    "kex_exchange_identification",
    "Broken pipe",
    "Control socket connect",
    "mux_client",
)


@dataclass(frozen=True, slots=True)
class SshSettings:
    target: SshTarget
    cwd: str = "~"
    timeout: float = 180.0
    persist_env: bool = True
    term_grace_s: float = 2.0
    temp_dir: str = "/tmp"


def _config(scope: Scope, key: str) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if value is None else value


def settings_from(scope: Scope, overrides: Mapping[str, Any] | None = None) -> SshSettings:
    """The SSH settings of ``scope`` (``terminal.ssh_*``), per-call overrides on top."""
    over = overrides or {}
    host = str(over.get("ssh_host") or _config(scope, "terminal.ssh_host") or "").strip()
    if not host:
        raise BackendUnavailable(
            "terminal.backend is ssh but terminal.ssh_host is not set "
            "(indus config set terminal.ssh_host <host>)"
        )
    user = str(over.get("ssh_user") or _config(scope, "terminal.ssh_user") or "").strip()
    try:
        port = int(over.get("ssh_port") or _config(scope, "terminal.ssh_port") or 22)
    except (TypeError, ValueError):
        raise BackendUnavailable("terminal.ssh_port must be a port number") from None
    key = str(over.get("ssh_key") or _config(scope, "terminal.ssh_key") or "").strip()
    if key.startswith("~/"):
        key = (scope.env.get("HOME") or os.path.expanduser("~")) + key[1:]
    raw_cwd = str(over.get("cwd") or _config(scope, "terminal.cwd") or "").strip()
    cwd = "~" if raw_cwd in ("", ".") else raw_cwd
    return SshSettings(
        target=SshTarget(host=host, user=user, port=port, key_path=key),
        cwd=cwd,
        timeout=float(_config(scope, "terminal.timeout")),
        persist_env=bool(_config(scope, "terminal.ssh_persistent_shell")),
        term_grace_s=float(_config(scope, "terminal.daemon_term_grace_s")),
    )


def connection_problem(text: str, target: SshTarget) -> tuple[str, str] | None:
    """(reason, retry hint) when ssh's own output says the connection failed, else None."""
    if any(sign in text for sign in _HOST_KEY_SIGNS):
        return (
            f"the host key of {target.label} does not match the one in known_hosts; Indus never "
            "accepts a changed host key",
            f"If the host was rebuilt on purpose, remove the old key with `ssh-keygen -R "
            f"{target.host}`, connect once by hand to check the new one, then retry.",
        )
    if any(sign in text for sign in _AUTH_SIGNS):
        used = (
            f"the key {target.key_path}"
            if target.key_path
            else "your ssh agent and default keys (terminal.ssh_key is not set)"
        )
        return (
            f"authentication to {target.destination} failed using {used}",
            f"Check that {target.user or 'the user'} can log in to {target.host} with that key "
            "without a passphrase prompt (BatchMode), then retry.",
        )
    for line in text.splitlines():
        if any(sign in line for sign in _LINK_SIGNS):
            return (
                f"cannot reach {target.label}: {line.strip()}",
                f"Check that {target.host} is up, that sshd listens on port {target.port} and "
                "that the network allows it; the next command reconnects automatically.",
            )
    return None


class SshExecProcess(LocalProcess):
    """The local ssh client plus where the remote shell wrote its pid."""

    def __init__(self, popen: subprocess.Popen[bytes], pid_file: str) -> None:
        super().__init__(popen)
        self.pid_file = pid_file


def remote_kill_script(pid_file: str, signal_name: str) -> str:
    """Signal the remote command's process group, its children and the shell itself."""
    q = shlex.quote(pid_file)
    return (
        f'p=$(cat {q} 2>/dev/null) || exit 0; [ -n "$p" ] || exit 0; '
        f'kill -{signal_name} -- -"$p" 2>/dev/null; pkill -{signal_name} -P "$p" 2>/dev/null; '
        f'kill -{signal_name} "$p" 2>/dev/null; exit 0'
    )


def _run_quietly(argv: list[str], env: Mapping[str, str]) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 - fixed client argv
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            timeout=REMOTE_KILL_TIMEOUT_S,
            check=False,
        )


class SshEnvironment(SpawnedEnvironment):
    """Commands on a remote host (see the module notes)."""

    backend_name = "ssh"

    def __init__(
        self,
        scope: Scope,
        settings: SshSettings,
        *,
        ssh: str,
        scp: str,
        session_id: str | None = None,
        clock: Clock | None = None,
        runner: Runner = run_client,
        os_name: str = os.name,
        max_chars: int = DEFAULT_MAX_CHARS,
        snapshot_timeout: float = SNAPSHOT_TIMEOUT_S,
        probe_timeout: float = PROBE_TIMEOUT_S,
        sync_files: bool = True,
    ) -> None:
        super().__init__(
            cwd=settings.cwd,
            timeout=settings.timeout,
            temp_dir=settings.temp_dir,
            flags=SSH_FLAGS,
            persist_env=settings.persist_env,
            clock=clock,
            capture_home=scope.home,
            max_chars=max_chars,
            snapshot_timeout=snapshot_timeout,
        )
        self.scope = scope
        self.settings = settings
        self.target = settings.target
        self.ssh = ssh
        self.scp = scp
        self.session_id = session_id
        self.runner = runner
        self.probe_timeout = probe_timeout
        self.sync_files = sync_files
        self.control_dir: str | None = None
        if multiplex_supported(os_name):
            directory = control_dir(scope.env.get("XDG_RUNTIME_DIR"), str(socket_dir(scope)))
            if socket_dir_usable(Path(directory)):
                self.control_dir = directory
            else:
                log.warning("ssh control dir %s is not private; multiplexing is off", directory)
        self.sockets: set[str] = set()
        self.remote_home = "/root" if self.target.user == "root" else f"/home/{self.target.user}"
        self.home_detected = False
        self._home_failed_at: float | None = None
        self.sync: FileSync | None = None

    # -- client pieces -------------------------------------------------------------------------

    def describe(self) -> str:
        return f"ssh to {self.target.label}, cwd {self._cwd}"

    def client_env(self) -> dict[str, str]:
        """The ssh client's own environment: the child env of this scope (passthrough values
        included, blocked names gone). Only the SendEnv names reach the remote side."""
        return child_env(self.scope, session_id=self.session_id)

    def passthrough(self, env: Mapping[str, str]) -> tuple[str, ...]:
        accepted, _ = passthrough_names(self.scope)
        return send_env_names(n for n in accepted if n in env)

    def _excluded_names(self) -> Iterable[str]:
        accepted, _ = passthrough_names(self.scope)
        return accepted

    def control_for(self, names: tuple[str, ...] = ()) -> str | None:
        if self.control_dir is None:
            return None
        path = control_path(self.control_dir, self.target, names)
        self.sockets.add(path)
        return path

    def argv(self, remote_command: str, names: tuple[str, ...] = ()) -> list[str]:
        return ssh_argv(
            self.ssh, self.target, remote_command, control=self.control_for(names), send_env=names
        )

    async def _client(self, remote_command: str, timeout: float) -> tuple[int, str]:
        """One short remote call on the plain master: (exit status, stdout + stderr)."""
        result = await self.runner(
            self.argv(remote_command), env=self.client_env(), timeout=timeout
        )
        return result.returncode, (result.stdout + result.stderr)

    def _connection_error(self, text: str) -> EnvironmentConnectionError:
        found = connection_problem(text, self.target)
        if found is not None:
            reason, hint = found
            return EnvironmentConnectionError(reason, retry_hint=hint)
        last = [line.strip() for line in text.splitlines() if line.strip()]
        detail = redact_output(last[-1], scope=self.scope) if last else "no message"
        return EnvironmentConnectionError(
            f"ssh to {self.target.label} failed: {detail}",
            retry_hint=(
                f"Check that {self.target.host} is reachable on port {self.target.port} and that "
                "the login works without prompts, then retry."
            ),
        )

    # -- opening ---------------------------------------------------------------------------------

    async def connect(self) -> None:
        """Probe, home, dirs, first push, snapshot (see the module notes)."""
        try:
            code, text = await self._client(f"echo {PROBE_TEXT}", self.probe_timeout)
        except TimeoutError:
            raise EnvironmentConnectionError(
                f"ssh to {self.target.label} timed out after {self.probe_timeout:g}s",
                retry_hint=(
                    f"Check that {self.target.host} is up and reachable on port "
                    f"{self.target.port}, then retry."
                ),
            ) from None
        if code != 0 or PROBE_TEXT not in text:
            raise self._connection_error(text)
        await self.detect_home()
        await self._make_dirs()
        if self.sync_files:
            self._build_sync()
            if self.sync is not None:
                await self.sync.sync(force=True)
        await self.start()

    async def detect_home(self) -> bool:
        """Ask for ``$HOME``; on failure keep the guess and note the time (retry spacing)."""
        try:
            code, text = await self._client('printf "%s\\n" "$HOME"', HOME_TIMEOUT_S)
        except TimeoutError:
            code, text = -1, ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        home = lines[-1] if lines else ""
        if code == 0 and home.startswith("/"):
            changed = home != self.remote_home
            self.remote_home = home
            self.home_detected = True
            self._home_failed_at = None
            if changed and self.sync is not None:
                self._build_sync()
            return True
        if code == SSH_LOST_RC and connection_problem(text, self.target) is not None:
            raise self._connection_error(text)
        self._home_failed_at = self.clock.monotonic()
        log.warning(
            "could not detect the home dir on %s; using %s for now",
            self.target.label,
            self.remote_home,
        )
        return False

    def home_retry_due(self) -> bool:
        if self.home_detected or self._home_failed_at is None:
            return False
        return self.clock.monotonic() - self._home_failed_at >= HOME_RETRY_S

    @property
    def remote_base(self) -> str:
        return posixpath.join(self.remote_home, REMOTE_APP_DIR)

    async def _make_dirs(self) -> None:
        dirs = " ".join(shlex.quote(posixpath.join(self.remote_base, d)) for d in REMOTE_SUBDIRS)
        try:
            code, text = await self._client(f"mkdir -p -- {dirs}", DIRS_TIMEOUT_S)
        except TimeoutError:
            log.warning("creating %s on %s timed out", self.remote_base, self.target.label)
            return
        if code != 0:
            log.warning("creating %s on %s failed: %s", self.remote_base, self.target.label, text)

    def sync_items(self) -> list[SyncItem]:
        credentials = resolve_credentials(self.scope, self.session_id)
        return collect_items(self.scope, self.remote_base, credentials)

    def _build_sync(self) -> None:
        transport = SshTransport(
            ssh=self.ssh,
            scp=self.scp,
            target=self.target,
            control=self.control_for(),
            env=self.client_env(),
            runner=self.runner,
            base=self.remote_base,
        )
        self.sync = FileSync(
            self.sync_items,
            transport,
            base=self.remote_base,
            clock=self.clock,
            env=self.scope.env,
        )

    # -- running -----------------------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        bounded: bool = True,
        on_activity: ActivityCallback | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        if not self._closed:
            if self.home_retry_due():
                await self.detect_home()
            if self.sync is not None:
                await self.sync.sync()
        return await super().execute(
            command,
            timeout=timeout,
            cwd=cwd,
            stdin=stdin,
            bounded=bounded,
            on_activity=on_activity,
            cancel=cancel,
        )

    async def _run_script(
        self,
        script: str,
        *,
        login: bool,
        timeout: float,
        stdin: str | None,
        bounded: bool,
        on_activity: ActivityCallback | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        result = await super()._run_script(
            script,
            login=login,
            timeout=timeout,
            stdin=stdin,
            bounded=bounded,
            on_activity=on_activity,
            cancel=cancel,
        )
        if (
            result.returncode == SSH_LOST_RC
            and not result.timed_out
            and not result.interrupted
            and connection_problem(result.output, self.target) is not None
        ):
            raise self._connection_error(result.output)
        return result

    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        env = self.client_env()
        names = self.passthrough(env)
        shell = "bash -l -c" if login else "bash -c"
        pid_file = posixpath.join(
            self.settings.temp_dir or "/tmp", f"indus-exec-{secrets.token_hex(6)}.pid"
        )
        q = shlex.quote(pid_file)
        head = f"printf '%s' \"$$\" > {q} 2>/dev/null; trap 'rm -f -- {q}' EXIT\n"
        argv = self.argv(f"{shell} {shlex.quote(head + script)}", names)
        popen = subprocess.Popen(  # noqa: S603 - argv built above; the command is the user's
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        proc = SshExecProcess(popen, pid_file)
        if stdin is not None:
            proc.start_stdin(stdin)
        return proc

    def _remote_kill_argv(self, proc: SshExecProcess, signal_name: str) -> list[str]:
        script = remote_kill_script(proc.pid_file, signal_name)
        return self.argv(f"sh -c {shlex.quote(script)}")

    async def _signal_remote(self, proc: SshExecProcess, signal_name: str, bound: float) -> None:
        if bound <= 0:
            return
        try:
            result = await self.runner(
                self._remote_kill_argv(proc, signal_name), env=self.client_env(), timeout=bound
            )
        except Exception as exc:
            log.debug("signalling the remote command failed: %s", type(exc).__name__)
            return
        if result.returncode != 0:
            log.debug("signalling the remote command failed: %s", result.stderr.strip())

    async def _kill(self, handle: ProcessHandle, within: float | None = None) -> None:
        proc = cast(SshExecProcess, handle)
        remote = REMOTE_KILL_TIMEOUT_S
        grace = self.settings.term_grace_s
        if within is not None:
            remote = min(remote, max(0.0, within / 4))
            grace = min(grace, max(0.0, within - 2 * remote - SURVIVOR_WINDOW_S))
        await self._signal_remote(proc, "TERM", remote)
        await stop_tree(proc, clock=self.clock, grace=grace)
        await self._signal_remote(proc, "KILL", remote)

    def _force_kill(self, handle: ProcessHandle) -> None:
        proc = cast(SshExecProcess, handle)
        force_kill_tree(proc)
        argv = self._remote_kill_argv(proc, "KILL")
        threading.Thread(
            target=_run_quietly, args=(argv, self.client_env()), name="ssh-kill", daemon=True
        ).start()

    # -- ending ------------------------------------------------------------------------------------

    async def cleanup(self) -> None:
        if self._closed:
            return
        await super().cleanup()
        env = self.client_env()
        if self.snapshot_ready:  # best effort: the remote temp dir is the host's to clean
            snap = shlex.quote(self.snapshot_path)
            with contextlib.suppress(Exception):
                await self.runner(
                    self.argv(f"rm -f -- {snap} {snap}.tmp.*"), env=env, timeout=EXIT_TIMEOUT_S
                )
        for socket in sorted(self.sockets):
            if not os.path.exists(socket):
                continue
            with contextlib.suppress(Exception):
                await self.runner(
                    exit_argv(self.ssh, self.target, socket), env=env, timeout=EXIT_TIMEOUT_S
                )
            with contextlib.suppress(OSError):
                os.unlink(socket)
        self.sockets.clear()


def setup_findings(scope: Scope) -> list[tuple[str, str, str | None]]:
    """What ``indus doctor`` reports for the ssh backend, without connecting:
    (severity, message, fix) rows."""
    try:
        settings = settings_from(scope)
    except BackendUnavailable as exc:
        return [("error", str(exc), "indus config set terminal.ssh_host <host>")]
    rows: list[tuple[str, str, str | None]] = []
    path = scope.env.get("PATH") or None
    missing = [name for name in ("ssh", "scp") if not locate.which(name, path=path)]
    if missing:
        rows.append(
            ("error", f"{' and '.join(missing)} not found on PATH", "install openssh-client")
        )
    key = settings.target.key_path
    if key and not os.path.isfile(key):
        rows.append(("error", f"terminal.ssh_key {key} does not exist", "fix terminal.ssh_key"))
    if not rows:
        rows.append(("ok", f"commands run on {settings.target.label} over ssh", None))
    return rows


async def open_ssh(
    scope: Scope,
    *,
    session_id: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    clock: Clock | None = None,
    runner: Runner = run_client,
    probe_timeout: float = PROBE_TIMEOUT_S,
) -> SshEnvironment:
    """A connected SSH environment for ``scope`` (``BackendUnavailable`` without a client or a
    host; ``EnvironmentConnectionError`` when the host cannot be reached)."""
    settings = settings_from(scope, overrides)
    path = scope.env.get("PATH") or None
    ssh = locate.which("ssh", path=path)
    scp = locate.which("scp", path=path)
    if not ssh or not scp:
        raise BackendUnavailable(
            "the ssh backend needs the OpenSSH client (ssh and scp) on PATH; install openssh-client"
        )
    env = SshEnvironment(
        scope,
        settings,
        ssh=ssh,
        scp=scp,
        session_id=session_id,
        clock=clock,
        runner=runner,
        probe_timeout=probe_timeout,
        max_chars=int(_config(scope, "tool_output.max_chars")),
    )
    # The local sudo password never goes to the remote host: a hook only with ssh_sudo_password.
    env.sudo = backend_sudo(
        scope, backend="ssh", target=f"ssh:{settings.target.label}", session_id=session_id
    )
    try:
        await env.connect()
    except BaseException:
        with contextlib.suppress(Exception):
            await env.cleanup()
        raise
    return env
