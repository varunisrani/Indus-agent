# pyright: strict
"""Malware preflight for stdio servers started through a package runner (P13-11).

``npx``, ``bunx`` and ``pnpm dlx`` fetch an npm package, ``uvx`` and ``pipx run`` a PyPI one, and
run it at once. Before such a server is spawned, the package (and its exact version when the
arguments pin one) is looked up in the OSV database; an advisory from the malicious-packages feed
(ids starting ``MAL-``) refuses the start with that id (``McpMalwareRefused``, permanent). The
lookup is bounded to ``WALL_BOUND_S`` on the injected clock and never blocks the event loop; a
timeout, a network error or an unreadable answer starts the server anyway, with one log line
(fail open). Answers are cached for 24 h per (ecosystem, name, version) in
``<home>/cache/mcp/osv.json``.

``guarded_opener`` wraps ``connect.open_session`` (lifecycle's ``Opener``) so the check runs in
the server's owner task, on the command and arguments after placeholders are filled, before the
spawn. ``mcp.osv_preflight: false`` turns it off.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx

from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_text
from indus.foundation.scope import Scope
from indus.mcp.config import McpError, McpServer, stdio_launch
from indus.mcp.connect import ConnectOptions, LiveSession, clock_timeout, open_session

__all__ = [
    "CACHE_TTL_S",
    "OSV_URL",
    "WALL_BOUND_S",
    "McpMalwareRefused",
    "OsvChecker",
    "PackageRef",
    "checker_for",
    "guarded_opener",
    "package_for",
    "preflight_enabled",
]

log = logging.getLogger(__name__)

OSV_URL = "https://api.osv.dev/v1/query"
WALL_BOUND_S = 12.0
HTTP_TIMEOUT_S = 10.0
CACHE_TTL_S = 24 * 3600.0
CACHE_NAME = "osv.json"
MALWARE_PREFIX = "MAL-"

Ecosystem = Literal["npm", "PyPI"]


class McpMalwareRefused(McpError, PermissionError):
    """The package a server would run has a malware advisory; the server was not started."""

    default_code = "mcp.malware_refused"

    def __init__(self, message: str, *, server: str = "", advisories: Sequence[str] = ()) -> None:
        McpError.__init__(
            self,
            message,
            server=server,
            hint="remove the server or point it at a package without an advisory",
        )
        self.advisories: tuple[str, ...] = tuple(advisories)


# -- which package a command would fetch --------------------------------------------------------


@dataclass(frozen=True)
class PackageRef:
    ecosystem: Ecosystem
    name: str
    version: str | None = None

    @property
    def key(self) -> str:
        return f"{self.ecosystem}:{self.name}@{self.version or ''}"

    def __str__(self) -> str:
        return f"{self.name}@{self.version}" if self.version else self.name


_EXACT_NPM = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")
_NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
_PYPI_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_PYPI_SPEC = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(?:(?P<op>==|@)\s*(?P<version>[^\s;,]+))?"
)

# Runner options that take a value (the value is not the package).
_NPX_VALUES = frozenset(
    {"-c", "--call", "--registry", "--cache", "--userconfig", "-w", "--workspace"}
)
_NPX_PACKAGE = frozenset({"-p", "--package"})
_UVX_VALUES = frozenset(
    {
        "--with",
        "--with-editable",
        "--with-requirements",
        "-p",
        "--python",
        "-c",
        "--constraints",
        "--overrides",
        "--index",
        "--index-url",
        "-i",
        "--extra-index-url",
        "--default-index",
        "--find-links",
        "-f",
        "--cache-dir",
        "--directory",
        "--project",
        "--config-file",
    }
)
_PIPX_VALUES = frozenset({"--python", "--index-url", "-i", "--pip-args"})


def _runner(command: str) -> str:
    base = os.path.basename(command.replace("\\", "/")).lower()
    for suffix in (".cmd", ".exe", ".ps1", ".bat"):
        base = base.removesuffix(suffix)
    return base


def _scan(
    args: Sequence[str], values: frozenset[str], named: frozenset[str]
) -> tuple[str | None, str | None]:
    """(package named by an option, first positional) in runner arguments."""
    package: str | None = None
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            rest = args[index + 1 :]
            return package, rest[0] if rest else None
        if word.startswith("-"):
            option, eq, inline = word.partition("=")
            if option in named:
                value = inline if eq else (args[index + 1] if index + 1 < len(args) else None)
                if package is None and value:
                    package = value
                index += 1 if eq else 2
                continue
            if option in values and not eq:
                index += 2
                continue
            index += 1
            continue
        return package, word
    return package, None


def _npm_ref(spec: str) -> PackageRef | None:
    text = spec.strip().removeprefix("npm:")
    if not text or text.startswith((".", "/", "~")) or ":" in text:
        return None
    at = text.rfind("@")
    name, version = (text[:at], text[at + 1 :]) if at > 0 else (text, "")
    name = name.lower()
    if not _NPM_NAME.match(name):
        return None
    exact = version.removeprefix("v") if _EXACT_NPM.match(version) else None
    return PackageRef("npm", name, exact)


def _pypi_ref(spec: str) -> PackageRef | None:
    text = spec.strip()
    if not text or text.startswith((".", "/", "~")) or "://" in text:
        return None
    match = _PYPI_SPEC.match(text)
    if match is None or not _PYPI_NAME.match(match.group("name")):
        return None
    name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
    version = match.group("version")
    if version and any(c in version for c in "*<>!~,"):
        version = None
    rest = text[match.end() :].strip()
    if rest and not rest.startswith(";"):
        version = None  # a range or several specifiers: no exact version
    return PackageRef("PyPI", name, version or None)


def package_for(command: str, args: Sequence[str]) -> PackageRef | None:
    """The package a runner command would fetch, or None for any other command."""
    runner = _runner(command)
    words = list(args)
    if runner == "pnpm":
        if not words or words[0] != "dlx":
            return None
        named, first = _scan(words[1:], frozenset(), frozenset({"--package"}))
        spec = named or first
        return _npm_ref(spec) if spec else None
    if runner in ("npx", "bunx"):
        named, first = _scan(words, _NPX_VALUES, _NPX_PACKAGE)
        spec = named or first
        return _npm_ref(spec) if spec else None
    if runner == "uvx":
        named, first = _scan(words, _UVX_VALUES, frozenset({"--from"}))
        spec = named or first
        return _pypi_ref(spec) if spec else None
    if runner == "pipx":
        if not words or words[0] != "run":
            return None
        named, first = _scan(words[1:], _PIPX_VALUES, frozenset({"--spec"}))
        spec = named or first
        return _pypi_ref(spec) if spec else None
    return None


# -- the lookup ---------------------------------------------------------------------------------


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=False)


def _malware_ids(payload: object) -> tuple[str, ...] | None:
    if not isinstance(payload, dict):
        return None
    vulns = cast(dict[str, Any], payload).get("vulns", [])
    if not isinstance(vulns, list):
        return None
    found: list[str] = []
    for entry in cast(list[object], vulns):
        if not isinstance(entry, dict):
            continue
        record = cast(dict[str, Any], entry)
        ident = str(record.get("id") or "")
        aliases_raw = record.get("aliases")
        aliases = (
            [str(a) for a in cast(list[object], aliases_raw)]
            if isinstance(aliases_raw, list)
            else []
        )
        if ident.startswith(MALWARE_PREFIX) or any(a.startswith(MALWARE_PREFIX) for a in aliases):
            found.append(ident)
    return tuple(found)


class OsvChecker:
    """OSV lookups with a 24 h cache for one home."""

    def __init__(
        self,
        home: Path,
        *,
        clock: Clock,
        client_factory: Callable[[], httpx.AsyncClient] = _default_client,
        url: str = OSV_URL,
    ) -> None:
        self.path = home / "cache" / "mcp" / CACHE_NAME
        self.clock = clock
        self.client_factory = client_factory
        self.url = url
        self.requests = 0
        self._cache: dict[str, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._cache is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            entries = cast(dict[str, Any], raw).get("entries") if isinstance(raw, dict) else None
            self._cache = (
                {k: v for k, v in cast(dict[str, Any], entries).items() if isinstance(v, dict)}
                if isinstance(entries, dict)
                else {}
            )
        return self._cache

    def _cached(self, ref: PackageRef) -> tuple[str, ...] | None:
        with self._lock:
            entry = self._load().get(ref.key)
        if entry is None:
            return None
        at = entry.get("at")
        if not isinstance(at, int | float) or self.clock.now().timestamp() - at > CACHE_TTL_S:
            return None
        ids = entry.get("ids")
        return tuple(str(i) for i in cast(list[object], ids)) if isinstance(ids, list) else None

    def _remember(self, ref: PackageRef, ids: tuple[str, ...]) -> None:
        now = self.clock.now().timestamp()
        with self._lock:
            cache = self._load()
            cache[ref.key] = {"at": now, "ids": list(ids)}
            for key in [k for k, v in cache.items() if now - float(v.get("at") or 0) > CACHE_TTL_S]:
                del cache[key]
            text = json.dumps({"version": 1, "entries": cache}, sort_keys=True, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, text + "\n")
        except OSError as exc:
            log.debug("the OSV cache was not written: %s", exc)

    async def _query(self, ref: PackageRef) -> tuple[str, ...]:
        body: dict[str, Any] = {"package": {"name": ref.name, "ecosystem": ref.ecosystem}}
        if ref.version:
            body["version"] = ref.version
        self.requests += 1
        async with self.client_factory() as client:
            response = await client.post(self.url, json=body)
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        ids = _malware_ids(response.json())
        if ids is None:
            raise ValueError("the answer is not an OSV result")
        return ids

    async def advisories(self, ref: PackageRef) -> tuple[str, ...] | None:
        """Malware advisory ids for ``ref`` (empty when clean); None when OSV could not say."""
        cached = self._cached(ref)
        if cached is not None:
            return cached
        try:
            async with clock_timeout(self.clock, WALL_BOUND_S):
                ids = await self._query(ref)
        except TimeoutError:
            log.warning(
                "OSV malware check for %s gave no answer within %.0f s; starting anyway",
                ref,
                WALL_BOUND_S,
            )
            return None
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("OSV malware check for %s failed (%s); starting anyway", ref, exc)
            return None
        self._remember(ref, ids)
        return ids

    async def preflight(self, name: str, command: str, args: Sequence[str]) -> PackageRef | None:
        """Check the package a runner command would fetch; raises ``McpMalwareRefused`` on a
        malware advisory. Returns the package checked (None when the command is no runner)."""
        ref = package_for(command, args)
        if ref is None:
            return None
        ids = await self.advisories(ref)
        if ids:
            shown = ", ".join(ids)
            raise McpMalwareRefused(
                f"MCP server {name!r} was not started: {ref} ({ref.ecosystem}) has a malware "
                f"advisory ({shown})",
                server=name,
                advisories=ids,
            )
        return ref


# -- wiring -------------------------------------------------------------------------------------

_checkers_lock = threading.Lock()
_checkers: dict[str, OsvChecker] = {}


def checker_for(scope: Scope) -> OsvChecker:
    """One checker (and in-memory cache) per home."""
    key = str(scope.home)
    with _checkers_lock:
        found = _checkers.get(key)
        if found is None:
            found = _checkers[key] = OsvChecker(scope.home, clock=scope.clock)
        return found


def preflight_enabled(scope: Scope) -> bool:
    if scope.config is None:
        return True
    try:
        value = scope.config.get("mcp.osv_preflight")
    except Exception:
        return True
    return value is not False


Opener = Callable[..., AbstractAsyncContextManager[LiveSession]]


def guarded_opener(
    inner: Opener = open_session,
    *,
    checker: Callable[[Scope], OsvChecker] = checker_for,
) -> Opener:
    """``inner`` with the malware preflight before every stdio spawn (lifecycle ``Opener``)."""

    @asynccontextmanager
    async def open_checked(
        name: str,
        server: McpServer,
        scope: Scope,
        *,
        clock: Clock,
        options: ConnectOptions | None = None,
    ) -> AsyncGenerator[LiveSession]:
        if server.kind == "stdio" and preflight_enabled(scope):
            workspace = options.workspace if options is not None else None
            launch = stdio_launch(name, server, scope, workspace=workspace)
            await checker(scope).preflight(name, launch.command, launch.args)
        async with inner(name, server, scope, clock=clock, options=options) as live:
            yield live

    return open_checked
