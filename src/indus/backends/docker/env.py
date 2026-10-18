# pyright: strict
"""Variables reach the container by name only (P11-04, R02.7).

``docker run`` / ``docker exec`` get ``-e NAME`` without a value; the docker client copies each
value from its own environment. A value on the command line would be readable by every local user
through ``/proc/<pid>/cmdline``; the client's environment is readable by its owner only.

Names come from ``terminal.docker_forward_env`` (values from the profile's secret store, else its
env view), ``terminal.docker_env`` (values from config) and the P05 passthrough names
(``terminal.env_passthrough`` and skill-declared names). A name Indus manages (provider keys,
``INDUS_*``, …) never passes, whatever the source (R02.22).

Forwarded and passthrough values are read again for every ``exec`` and those names are kept out
of the shell snapshot, so a changed secret is seen at once and one container serving several
sessions always gets the current values; a name without a value now is unset in the command, so
a value baked into the container when it was created cannot leak.

The client's own environment: the minimal child environment of the P05 builder, plus the
variables the client itself reads (``DOCKER_*``, Podman's ``CONTAINER_HOST``, ``XDG_RUNTIME_DIR``,
proxies), plus the values.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from indus.backends.docker.settings import DockerSettings
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env, is_blocked, passthrough_names

__all__ = [
    "EnvPlan",
    "client_env",
    "managed_names",
    "name_flags",
    "plan_env",
    "redact_values",
    "unset_prefix",
]

log = logging.getLogger(__name__)

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CLIENT_PREFIXES = ("DOCKER_", "CONTAINER_", "CONTAINERS_", "PODMAN_")
CLIENT_NAMES = (
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
MASK = "[redacted]"
_MIN_MASKED = 3

_warned_lock = threading.Lock()
_warned: set[str] = set()


@dataclass(frozen=True, slots=True)
class EnvPlan:
    """``run_names``: ``-e`` names for ``docker run``; ``exec_names``: ``-e`` names for every
    ``exec``; ``values``: what the client env carries; ``unset``: names the command must not see;
    ``managed``: names kept out of the snapshot; ``refused``: blocked names that were asked for."""

    run_names: tuple[str, ...] = ()
    exec_names: tuple[str, ...] = ()
    values: Mapping[str, str] = field(default_factory=lambda: {})
    unset: tuple[str, ...] = ()
    managed: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()


def _warn_once(key: str, message: str, *args: object) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(message, *args)


def _valid(names: Iterable[str], source: str) -> list[str]:
    out: list[str] = []
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        if not NAME.match(name):
            _warn_once(
                f"bad:{source}:{name}", "%s: %r is not a variable name; ignored", source, raw
            )
            continue
        if name not in out:
            out.append(name)
    return out


def _scope_value(scope: Scope, name: str) -> str | None:
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception as exc:
            log.debug("secret lookup for %s failed: %s", name, type(exc).__name__)
            value = None
        if value is not None:
            return value
    return scope.env.get(name)


def plan_env(scope: Scope, settings: DockerSettings) -> EnvPlan:
    """The names and values for ``scope`` right now (see the module notes)."""
    refused: list[str] = []

    def allowed(name: str, source: str) -> bool:
        if is_blocked(name, scope):
            refused.append(name)
            _warn_once(
                f"blocked:{source}:{name}",
                "%s lists %s, a credential Indus manages; it never enters the container",
                source,
                name,
            )
            return False
        return True

    forward = [
        n
        for n in _valid(settings.forward_env, "terminal.docker_forward_env")
        if allowed(n, "terminal.docker_forward_env")
    ]
    accepted, _ = passthrough_names(scope)
    dynamic = list(dict.fromkeys([*forward, *_valid(accepted, "env passthrough")]))
    explicit = {
        n: settings.env[n]
        for n in _valid(settings.env.keys(), "terminal.docker_env")
        if allowed(n, "terminal.docker_env")
    }

    values: dict[str, str] = dict(explicit)
    exec_names: list[str] = []
    unset: list[str] = []
    for name in dynamic:
        value = _scope_value(scope, name)
        if value is None:
            unset.append(name)
        else:
            values[name] = value
            exec_names.append(name)
    run_names = sorted({*explicit, *exec_names})
    return EnvPlan(
        run_names=tuple(run_names),
        exec_names=tuple(sorted(exec_names)),
        values=values,
        unset=tuple(sorted(unset)),
        managed=tuple(sorted(dynamic)),
        refused=tuple(dict.fromkeys(refused)),
    )


def managed_names(scope: Scope, settings: DockerSettings) -> tuple[str, ...]:
    """The names read again for every ``exec`` (kept out of the snapshot); no values read."""
    forward = [
        n
        for n in _valid(settings.forward_env, "terminal.docker_forward_env")
        if not is_blocked(n, scope)
    ]
    accepted, _ = passthrough_names(scope)
    return tuple(sorted({*forward, *_valid(accepted, "env passthrough")}))


def name_flags(names: Iterable[str]) -> list[str]:
    """``-e NAME`` pairs, never ``NAME=value``."""
    out: list[str] = []
    for name in sorted(set(names)):
        out += ["-e", name]
    return out


def client_env(scope: Scope, values: Mapping[str, str] | None = None) -> dict[str, str]:
    """The docker client's own environment (see the module notes)."""
    control = {
        name: value
        for name, value in scope.env.items()
        if name.startswith(CLIENT_PREFIXES) or name in CLIENT_NAMES
    }
    env = child_env(scope, variant="minimal", extra=control)
    env.update(values or {})
    return env


def unset_prefix(names: Iterable[str]) -> str:
    """A script line that removes ``names`` from the shell (empty when there are none)."""
    wanted = sorted({n for n in names if NAME.match(n)})
    if not wanted:
        return ""
    return f"unset {' '.join(shlex.quote(n) for n in wanted)} 2>/dev/null || true\n"


def redact_values(text: str, values: Iterable[str]) -> str:
    """``text`` with every value of ``values`` (3 characters or more) masked."""
    for value in sorted({v for v in values if len(v) >= _MIN_MASKED}, key=len, reverse=True):
        text = text.replace(value, MASK)
    return text
