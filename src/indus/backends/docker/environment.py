# pyright: strict
"""The Docker backend: one long-lived container, a fresh ``bash -c`` per command (P11-05).

Lifecycle. A container runs ``sleep infinity`` with Indus's labels: ``indus-agent=1``,
``indus-task-id`` (``shared`` for a profile-wide container, else the session), ``indus-profile``
and ``indus-environment``, a fingerprint of the image, the mounts and the profile home (left out
when ``docker_shared_key`` makes several profiles share one container). With
``docker_persist_across_processes`` a new process first attaches to a matching container (running
preferred, a stopped one is started); otherwise it creates one named ``indus-<hex8>``. A failed
``docker run`` removes the half-made container by name, and its error text never shows a variable
value.

Commands go through the shared shell flow of P05 (``SpawnedEnvironment``: snapshot, wrapper, cwd
marker, capture, wait, backstop). The transport is ``docker exec [-i] -e NAME… <id> bash -c
<script>`` with the values in the client's environment (``env.py``). The container's working
directory is set at ``run``; ``exec`` gets no ``-w`` because the wrapper changes directory itself
and a ``-w`` to a removed directory would fail before bash starts. Every script first writes its
shell's pid to a file in the container, so stopping a command reaches inside the container too
(TERM, the P05 ordered kill of the local client, then KILL).

Keys: ``terminal.container_persistent`` on → one environment per profile (registry key kind
``profile``); off → one per session. Cleanup leaves a persistent, reusable container running for
the next process (the orphan reaper sweeps leftovers, ``reaper.py``); otherwise it is stopped
(``stop -t 10``) and removed. A client that cannot reach its daemon raises a connection error:
the registry answers "degraded" and the next call starts over. A container that vanished is
recovered by ``recovery.py``.

Read-only mounts: the profile's skills (``/root/.indus/skills``), its media cache
(``/root/.indus/cache/media``, created when missing: the media tools name generated files there
for a Docker terminal, P10-16/17), credential files and registered sources.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import (
    ActivityCallback,
    BackendError,
    CancelSignal,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.docker import recovery
from indus.backends.docker.args import run_args
from indus.backends.docker.env import (
    client_env,
    managed_names,
    name_flags,
    plan_env,
    redact_values,
    unset_prefix,
)
from indus.backends.docker.mounts import (
    MountPlan,
    ReadonlyMount,
    credential_mounts,
    plan_mounts,
    readonly_from_sources,
)
from indus.backends.docker.probe import (
    CliResult,
    DockerCli,
    DockerDaemonDown,
    Runner,
    ensure_daemon,
    host_facts,
    require_client,
    start_hint,
)
from indus.backends.docker.settings import DockerSettings, settings_for
from indus.backends.local import SURVIVOR_WINDOW_S, LocalProcess, force_kill_tree, stop_tree
from indus.backends.registry import CreateRequest
from indus.backends.spawned import SNAPSHOT_TIMEOUT_S, SpawnedEnvironment
from indus.backends.sudo import backend_sudo
from indus.backends.wait import ProcessHandle
from indus.foundation.clock import Clock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope

__all__ = [
    "LABEL_AGENT",
    "LABEL_ENVIRONMENT",
    "LABEL_PROFILE",
    "LABEL_TASK",
    "SHARED_TASK",
    "ContainerLifecycle",
    "DockerEnvironment",
    "fingerprint",
    "label_value",
    "open_docker",
    "profile_label",
]

log = logging.getLogger(__name__)

LABEL_AGENT = "indus-agent"
LABEL_TASK = "indus-task-id"
LABEL_PROFILE = "indus-profile"
LABEL_ENVIRONMENT = "indus-environment"
SHARED_TASK = "shared"
SANDBOX_TEMP = "/tmp"  # noqa: S108 - inside the container (a size-capped tmpfs)
SKILLS_TARGET = "/root/.indus/skills"
# Generated images, video and speech (P10-16/17) are cached under <home>/cache/media; the media
# tools report this in-container name for them (``sandbox_path``), so the folder is mounted.
MEDIA_DIR = Path("cache") / "media"
MEDIA_TARGET = "/root/.indus/cache/media"
RUN_TIMEOUT_S = 120.0  # a first run may pull the image
PS_TIMEOUT_S = 10.0
START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 30.0
RM_TIMEOUT_S = 30.0
INSIDE_KILL_TIMEOUT_S = 2.0


# -- names ----------------------------------------------------------------------------------------


def label_value(text: str) -> str:
    """A value ``docker ps --filter label=k=v`` round-trips: ``[A-Za-z0-9_.-]``, ≤ 63 chars."""
    cleaned = "".join(c if c.isascii() and (c.isalnum() or c in "_.-") else "_" for c in text)
    return cleaned[:63] or "unknown"


def profile_label(scope: Scope, shared_key: str = "") -> str:
    """The profile identity on a container: the profile id, or the shared key (with a digest of
    the raw key, since cleaning it is lossy)."""
    if not shared_key:
        return label_value(scope.profile_id)
    digest = hashlib.sha256(shared_key.encode("utf-8")).hexdigest()[:12]
    return f"{label_value(shared_key)[:50]}-{digest}"


def fingerprint(image: str, mount_args: tuple[str, ...], home: str) -> str:
    """A hash of what cannot change on a running container: image, mounts (in order), home."""
    payload = json.dumps(
        {"image": image, "mounts": list(mount_args), "home": os.path.normpath(home)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# -- the container --------------------------------------------------------------------------------


class ContainerLifecycle:
    """Find, start, create and remove this environment's container."""

    def __init__(
        self,
        cli: DockerCli,
        *,
        image: str,
        labels: Mapping[str, str],
        run_args: tuple[str, ...],
        init: bool,
        workdir: str,
        network: bool,
        values: Callable[[], Mapping[str, str]],
    ) -> None:
        self.cli = cli
        self.image = image
        self.labels = dict(labels)
        self.run_args = run_args
        self.init = init
        self.workdir = workdir
        self.network = network
        self.values = values

    def _daemon_check(self, result: CliResult) -> None:
        if recovery.daemon_gone(result.text, result.returncode):
            raise DockerDaemonDown(
                f"the {self.cli.client.label} daemon stopped answering",
                retry_hint=start_hint(self.cli.client).capitalize(),
            )

    async def find_reusable(self) -> tuple[str, str] | None:
        """(id, state) of a container with exactly these labels; a running one first."""
        filters: list[str] = []
        for key, value in sorted(self.labels.items()):
            filters += ["--filter", f"label={key}={value}"]
        result = await self.cli.call(
            "ps", "-a", *filters, "--format", "{{.ID}}\t{{.State}}", timeout=PS_TIMEOUT_S
        )
        self._daemon_check(result)
        if not result.ok:
            log.debug("looking for a reusable container failed: %s", result.text)
            return None
        first: tuple[str, str] | None = None
        for line in result.stdout.splitlines():
            cid, _, state = line.strip().partition("\t")
            if not cid or not state:
                continue
            found = (cid, state.strip().lower())
            if found[1] == "running":
                return found
            first = first or found
        return first

    async def start(self, cid: str) -> bool:
        result = await self.cli.call("start", cid, timeout=START_TIMEOUT_S)
        self._daemon_check(result)
        if not result.ok:
            log.warning("could not start container %s: %s", cid[:12], result.text)
        return result.ok

    async def network_mode(self, cid: str) -> str | None:
        result = await self.cli.call(
            "inspect", "--format", "{{.HostConfig.NetworkMode}}", cid, timeout=PS_TIMEOUT_S
        )
        return result.stdout.strip() or None if result.ok else None

    async def remove(self, cid: str) -> None:
        result = await self.cli.call("rm", "-f", cid, timeout=RM_TIMEOUT_S)
        if not result.ok:
            log.warning("could not remove container %s: %s", cid[:12], result.text)

    async def stop_and_remove(self, cid: str) -> None:
        stopped = await self.cli.call("stop", "-t", "10", cid, timeout=STOP_TIMEOUT_S)
        if not stopped.ok:
            log.debug("docker stop %s: %s", cid[:12], stopped.text)
        await self.remove(cid)

    def run_argv(self, name: str) -> list[str]:
        labels: list[str] = []
        for key, value in self.labels.items():
            labels += ["--label", f"{key}={value}"]
        return [
            "run",
            "-d",
            *(["--init"] if self.init else []),
            "--name",
            name,
            *labels,
            "-w",
            self.workdir,
            *self.run_args,
            self.image,
            "sleep",
            "infinity",
        ]

    async def create(self) -> str:
        """A fresh container; on failure the half-made one is removed by name."""
        name = f"indus-{secrets.token_hex(4)}"
        values = self.values()
        result = await self.cli.call(*self.run_argv(name), timeout=RUN_TIMEOUT_S, extra_env=values)
        self._daemon_check(result)
        if not result.ok:
            await self.cli.call("rm", "-f", name, timeout=RM_TIMEOUT_S)
            why = "timed out" if result.timed_out else redact_values(result.text, values.values())
            raise BackendError(
                f"the sandbox container could not be started: {why}",
                hint="check terminal.docker_image and terminal.docker_extra_args, then try again",
            )
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            raise BackendError("the container client started a container but printed no id")
        log.info("started sandbox container %s (%s)", name, lines[-1][:12])
        return lines[-1]

    async def attach_or_create(self, *, reuse: bool) -> tuple[str, bool]:
        """(container id, attached to an existing one)."""
        if reuse:
            found = await self.find_reusable()
            if found is not None:
                cid, state = found
                usable = True
                if not self.network:
                    mode = await self.network_mode(cid)
                    if mode != "none":
                        log.warning(
                            "container %s has network %s but docker_network is false; replacing it",
                            cid[:12],
                            mode or "unknown",
                        )
                        await self.remove(cid)
                        usable = False
                if usable and (state == "running" or await self.start(cid)):
                    log.info("reusing sandbox container %s (was %s)", cid[:12], state)
                    return cid, True
        return await self.create(), False


class DockerExecProcess(LocalProcess):
    """The local ``docker exec`` client plus where its shell wrote its pid in the container."""

    def __init__(self, popen: subprocess.Popen[bytes], pid_file: str) -> None:
        super().__init__(popen)
        self.pid_file = pid_file


def _inside_kill_script(pid_file: str, signal_name: str) -> str:
    return (
        f'p=$(cat {pid_file} 2>/dev/null) || exit 0; [ -n "$p" ] || exit 0; '
        f'kill -{signal_name} -- -"$p" 2>/dev/null; pkill -{signal_name} -P "$p" 2>/dev/null; '
        f'kill -{signal_name} "$p" 2>/dev/null; exit 0'
    )


# -- the environment ------------------------------------------------------------------------------


class DockerEnvironment(SpawnedEnvironment):
    """Commands in a sandbox container (see the module notes)."""

    backend_name = "docker"

    def __init__(
        self,
        scope: Scope,
        *,
        cli: DockerCli,
        settings: DockerSettings,
        lifecycle: ContainerLifecycle,
        mounts: MountPlan,
        cwd: str,
        timeout: float,
        env_key: str,
        session_id: str | None = None,
        persist_env: bool = True,
        term_grace_s: float = 2.0,
        clock: Clock | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        snapshot_timeout: float = SNAPSHOT_TIMEOUT_S,
    ) -> None:
        super().__init__(
            cwd=cwd,
            timeout=timeout,
            temp_dir=SANDBOX_TEMP,
            flags=recovery.isolated_flags(mounts.host_paths_mounted),
            persist_env=persist_env,
            clock=clock,
            capture_home=scope.home,
            max_chars=max_chars,
            snapshot_timeout=snapshot_timeout,
        )
        self.scope = scope
        self.cli = cli
        self.settings = settings
        self.lifecycle = lifecycle
        self.mounts = mounts
        self.env_key = env_key
        self.session_id = session_id
        self.term_grace_s = term_grace_s
        self._container_id: str | None = None
        # How many times the container was replaced; unlike the one-shot result flag, pollers of
        # remote background jobs can compare it at any time.
        self.recreations = 0
        # The P05 sudo rewrite feeds a password through stdin; the host's never reaches a
        # container. ``open_docker`` installs a hook only for a stored docker_sudo_password.

    # -- the container behind it

    @property
    def container_id(self) -> str | None:
        return self._container_id

    def adopt_container(self, cid: str) -> None:
        self._container_id = cid

    def forget_container(self) -> None:
        self._container_id = None

    def describe(self) -> str:
        cid = (self._container_id or "none")[:12]
        return f"{self.cli.client.label} container {cid} ({self.settings.image}), cwd {self._cwd}"

    async def restart_snapshot(self) -> None:
        """Take the snapshot again (a new container has none)."""
        self.snapshot_ready = False
        self.prefer_nonlogin = False
        await self.start()

    # -- spawning

    def _excluded_names(self) -> tuple[str, ...]:
        return managed_names(self.scope, self.settings)

    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        cid = self._container_id
        if cid is None:
            raise BackendError("the sandbox container is not running")
        plan = plan_env(self.scope, self.settings)
        pid_file = f"{SANDBOX_TEMP}/indus-exec-{secrets.token_hex(6)}.pid"
        head = f"printf '%s' \"$$\" > {pid_file} 2>/dev/null; trap 'rm -f -- {pid_file}' EXIT\n"
        argv = self.cli.argv(
            "exec",
            *(["-i"] if stdin is not None else []),
            *name_flags(plan.exec_names),
            cid,
            "bash",
            *(["-l"] if login else []),
            "-c",
            head + unset_prefix(plan.unset) + script,
        )
        try:
            popen = subprocess.Popen(  # noqa: S603 - the client argv carries names, never values
                argv,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.cli.client_env(plan.values),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise EnvironmentConnectionError(
                f"the {self.cli.client.label} client could not be run ({exc.strerror or exc})"
            ) from exc
        proc = DockerExecProcess(popen, pid_file)
        if stdin is not None:
            proc.start_stdin(stdin)
        return proc

    def _inside_argv(self, proc: DockerExecProcess, signal_name: str) -> list[str] | None:
        cid = self._container_id
        if cid is None:
            return None
        return ["exec", cid, "sh", "-c", _inside_kill_script(proc.pid_file, signal_name)]

    async def _signal_inside(self, proc: DockerExecProcess, signal_name: str, bound: float) -> None:
        args = self._inside_argv(proc, signal_name)
        if args is None or bound <= 0:
            return
        result = await self.cli.call(*args, timeout=bound)
        if not result.ok:
            log.debug("signalling inside the container failed: %s", result.text)

    async def _kill(self, handle: ProcessHandle, within: float | None = None) -> None:
        proc = cast(DockerExecProcess, handle)
        inside = INSIDE_KILL_TIMEOUT_S
        grace = self.term_grace_s
        if within is not None:
            inside = min(inside, max(0.0, within / 4))
            grace = min(grace, max(0.0, within - 2 * inside - SURVIVOR_WINDOW_S))
        await self._signal_inside(proc, "TERM", inside)
        await stop_tree(proc, clock=self.clock, grace=grace)
        await self._signal_inside(proc, "KILL", inside)

    def _force_kill(self, handle: ProcessHandle) -> None:
        proc = cast(DockerExecProcess, handle)
        force_kill_tree(proc)
        args = self._inside_argv(proc, "KILL")
        if args is None:
            return
        argv = self.cli.argv(*args)
        env = self.cli.client_env()
        threading.Thread(
            target=_run_quietly, args=(argv, env), name="docker-kill", daemon=True
        ).start()

    # -- running

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
            not result.timed_out
            and not result.interrupted
            and recovery.daemon_gone(result.output, result.returncode)
        ):
            raise DockerDaemonDown(
                f"the {self.cli.client.label} daemon stopped answering",
                retry_hint=start_hint(self.cli.client).capitalize(),
            )
        return result

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
        options: dict[str, Any] = {
            "timeout": timeout,
            "cwd": cwd,
            "stdin": stdin,
            "bounded": bounded,
            "on_activity": on_activity,
            "cancel": cancel,
        }
        result = await super().execute(command, **options)
        if not recovery.container_gone(result.output, result.returncode):
            return result
        if not self.settings.persist_across_processes:
            raise EnvironmentConnectionError(
                "the sandbox container is gone (removed or stopped outside Indus)",
                retry_hint="Run the command again; a new sandbox container is started for it.",
            )
        if not await recovery.recover(self):
            return result
        return await super().execute(command, **options)

    async def cleanup(self) -> None:
        cid = self._container_id
        self._container_id = None
        await super().cleanup()
        if cid is None:
            return
        if self.settings.persist_across_processes and self.settings.persistent:
            # The container outlives us: take our snapshot out of its /tmp.
            snapshot = self.snapshot_path
            await self.cli.call(
                "exec", cid, "sh", "-c", f"rm -f {snapshot} {snapshot}.tmp.*",
                timeout=INSIDE_KILL_TIMEOUT_S,
            )  # fmt: skip
            log.info("leaving sandbox container %s running for the next run", cid[:12])
            return
        await self.lifecycle.stop_and_remove(cid)


def _run_quietly(argv: list[str], env: Mapping[str, str]) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 - fixed client argv
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            timeout=INSIDE_KILL_TIMEOUT_S,
            check=False,
        )


# -- opening --------------------------------------------------------------------------------------


def _config(scope: Scope, key: str) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if value is None else value


def skills_mounts(scope: Scope) -> list[ReadonlyMount]:
    skills = scope.home / "skills"
    return [ReadonlyMount(str(skills), SKILLS_TARGET, "dir")] if skills.is_dir() else []


def media_mounts(scope: Scope) -> list[ReadonlyMount]:
    """The profile's media cache, read-only (created first, so files made later show up)."""
    media = scope.home / MEDIA_DIR
    try:
        media.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("the media cache %s could not be created; not mounted", media)
        return []
    return [ReadonlyMount(str(media), MEDIA_TARGET, "dir")]


def _host_cwd(scope: Scope, override: object) -> str:
    raw = str(override) if override else str(_config(scope, "terminal.cwd") or ".")
    home = scope.env.get("HOME") or ""
    if home and (raw == "~" or raw.startswith("~/")):
        raw = home + raw[1:]
    return raw if os.path.isabs(raw) else os.path.abspath(raw)


type ReaperStarter = Callable[[DockerCli, Scope, str, str], object]


async def open_docker(
    request: CreateRequest,
    *,
    runner: Runner | None = None,
    os_name: str = sys.platform,
    clock: Clock | None = None,
    reaper: ReaperStarter | None = None,
) -> DockerEnvironment:
    """A started Docker environment for ``request`` (the registry's factory)."""
    scope = request.scope
    settings = settings_for(scope)
    client = require_client(scope, settings.binary)
    cli = DockerCli(client, client_env(scope), runner=runner)
    await ensure_daemon(cli, os_name=os_name)
    facts = await host_facts(cli, settings.image, os_name=os_name)

    task_key = SHARED_TASK if request.key.kind == "profile" else request.key.ident
    override = request.overrides.get("cwd")
    mounts = plan_mounts(
        settings,
        home=scope.home,
        task_key=task_key,
        host_cwd=_host_cwd(scope, override),
        user_home=scope.env.get("HOME"),
        readonly=[
            *skills_mounts(scope),
            *media_mounts(scope),
            *credential_mounts(scope, request.session_id),
            *readonly_from_sources(scope),
        ],
    )
    run = run_args(settings, facts)
    run_env = plan_env(scope, settings)
    labels = {
        LABEL_AGENT: "1",
        LABEL_TASK: label_value(task_key),
        LABEL_PROFILE: profile_label(scope, settings.shared_key),
    }
    if not settings.shared_key:
        labels[LABEL_ENVIRONMENT] = fingerprint(settings.image, mounts.args, str(scope.home))
    if mounts.cwd_target:
        cwd = mounts.cwd_target
    elif override and str(override).startswith("/"):
        cwd = str(override)
    else:
        cwd = mounts.default_cwd

    lifecycle = ContainerLifecycle(
        cli,
        image=settings.image,
        labels=labels,
        run_args=(*run.args, *mounts.args, *name_flags(run_env.run_names), *run.extra),
        init=run.init,
        workdir=cwd,
        network=settings.network,
        values=lambda: plan_env(scope, settings).values,
    )
    env = DockerEnvironment(
        scope,
        cli=cli,
        settings=settings,
        lifecycle=lifecycle,
        mounts=mounts,
        cwd=cwd,
        timeout=float(_config(scope, "terminal.timeout")),
        env_key=str(request.key),
        session_id=request.session_id,
        persist_env=bool(_config(scope, "terminal.persistent_shell")),
        term_grace_s=float(_config(scope, "terminal.daemon_term_grace_s")),
        clock=clock,
        max_chars=int(_config(scope, "tool_output.max_chars")),
    )
    env.sudo = backend_sudo(
        scope, backend="docker", target=f"docker:{request.key}", session_id=request.session_id
    )
    cid, _ = await lifecycle.attach_or_create(reuse=settings.persist_across_processes)
    env.adopt_container(cid)
    try:
        await env.start()
    except BaseException:
        with contextlib.suppress(Exception):
            await env.cleanup()
        raise
    if settings.orphan_reaper:
        start = reaper if reaper is not None else _default_reaper
        try:
            start(cli, scope, labels[LABEL_PROFILE], cid)
        except Exception:
            log.warning("could not start the orphan container sweep", exc_info=True)
    return env


def _default_reaper(cli: DockerCli, scope: Scope, profile: str, keep: str) -> object:
    from indus.backends.docker.reaper import start_reaper_once

    return start_reaper_once(cli, scope, profile_label=profile, keep={keep})
