"""Package definitions, the machine's target and ``ensure`` (P24-23).

``Pm(root)`` manages ``<root>/pm/``: ``lock.json`` (pins), ``facts.json`` (what is installed),
``store/`` (unpacked entries), ``downloads/`` (verified archives by sha256, partial downloads) and
``.pm.lock`` (one installer at a time per root).

``ensure(name)`` installs ``name`` and its dependencies when the facts do not match the lock and
returns a ``Runner``. When everything matches, the only file-system work is a ``stat`` of the lock,
the facts file and each entry folder: both files are held in a ``StatCache`` and the definitions
are parsed once per process. The fetcher (and with it httpx) is imported only when something has
to be downloaded.

A ``Runner`` composes its child environment with the caller's env builder
(``indus.safety.child_env.child_env``; R02.22): ``indus.pm`` sits beside ``indus.safety`` in the
layer order, so the builder is passed in, never imported here.
"""

from __future__ import annotations

import functools
import os
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.fsio import FileLock
from indus.foundation.scope import Scope
from indus.pm.lock import ANY, TARGETS, Fact, Facts, Lock, Pin, PmError, StatCache
from indus.pm.store import FORMATS, Store, entry_name, tree_digest, unpack

if TYPE_CHECKING:
    import httpx

    from indus.pm.fetch import Fetcher

__all__ = [
    "KINDS",
    "EnvBuilder",
    "Package",
    "Paths",
    "Pm",
    "Runner",
    "current_target",
    "definitions",
    "parse_definitions",
]

type Kind = Literal["required", "default", "optional"]
KINDS: tuple[Kind, ...] = ("required", "default", "optional")
INSTALL_LOCK_TIMEOUT_S = 1800.0
MUSL_LOADERS = ("/lib/ld-musl-x86_64.so.1", "/lib/ld-musl-aarch64.so.1")


class EnvBuilder(Protocol):
    """``indus.safety.child_env.child_env`` (or a test double)."""

    def __call__(
        self, scope: Scope, *, extra: Mapping[str, str] | None = ...
    ) -> dict[str, str]: ...


# -- definitions -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    name: str
    summary: str
    kind: Kind
    version: str
    url: str
    format: str
    strip: int
    exe: str
    path: tuple[str, ...] | None
    depends: tuple[str, ...]
    releases: str
    tag_prefix: str
    targets: Mapping[str, Mapping[str, Any]]

    def ships_for(self, target: str) -> bool:
        return target in self.targets or ANY in self.targets

    def _values(self, target: str) -> Mapping[str, Any]:
        found = self.targets.get(target)
        if found is None:
            found = self.targets.get(ANY)
        if found is None:
            raise PmError(f"{self.name} is not available for {target}")
        return found

    def _fill(self, template: str, version: str, target: str) -> str:
        values = {k: str(v) for k, v in self._values(target).items()}
        try:
            return template.format(version=version, **values)
        except (KeyError, IndexError, ValueError) as exc:
            raise PmError(f"{self.name}: template {template!r} needs {exc}") from exc

    def url_for(self, version: str, target: str) -> str:
        values = self._values(target)
        return self._fill(str(values.get("url", self.url)), version, target)

    def format_for(self, target: str) -> str:
        return str(self._values(target).get("format", self.format))

    def strip_for(self, target: str) -> int:
        return int(self._values(target).get("strip", self.strip))

    def exe_for(self, version: str, target: str) -> str | None:
        raw = str(self._values(target).get("exe", self.exe))
        if not raw:
            return None
        name = self._fill(raw, version, target)
        if target.startswith("win32") and not name.lower().endswith(".exe"):
            name += ".exe"
        return name

    def path_for(self, version: str, target: str) -> tuple[str, ...]:
        raw = self._values(target).get("path", self.path)
        if raw is not None:
            return tuple(str(p) for p in cast(Sequence[object], raw))
        exe = self.exe_for(version, target)
        if exe is None:
            return ()
        parent = Path(exe).parent.as_posix()
        return (parent,)


def _package(name: str, raw: Mapping[str, Any]) -> Package:
    def text(key: str, default: str | None = None) -> str:
        value = raw.get(key, default)
        if not isinstance(value, str):
            raise PmError(f"packages.toml: {name}.{key} must be text")
        return value

    kind = text("kind")
    if kind not in KINDS:
        raise PmError(f"packages.toml: {name}.kind must be one of {', '.join(KINDS)}")
    fmt = text("format")
    targets_raw = raw.get("targets", {})
    if not isinstance(targets_raw, Mapping) or not targets_raw:
        raise PmError(f"packages.toml: {name} names no targets")
    targets: dict[str, Mapping[str, Any]] = {}
    for target, values in cast(Mapping[str, object], targets_raw).items():
        if target not in TARGETS:
            raise PmError(f"packages.toml: {name} names an unknown target {target!r}")
        if not isinstance(values, Mapping):
            raise PmError(f"packages.toml: {name}.targets.{target} must be a table")
        row = cast(Mapping[str, Any], values)
        if str(row.get("format", fmt)) not in FORMATS:
            raise PmError(f"packages.toml: {name} has an unknown format for {target}")
        targets[target] = dict(row)
    path = raw.get("path")
    depends = raw.get("depends", [])
    return Package(
        name=name,
        summary=text("summary", ""),
        kind=cast(Kind, kind),
        version=text("version"),
        url=text("url"),
        format=fmt,
        strip=int(raw.get("strip", 0)),
        exe=text("exe", ""),
        path=tuple(str(p) for p in cast(list[object], path)) if isinstance(path, list) else None,
        depends=tuple(str(d) for d in cast(list[object], depends)),
        releases=text("releases", "pinned"),
        tag_prefix=text("tag_prefix", ""),
        targets=targets,
    )


def parse_definitions(text: str) -> dict[str, Package]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PmError(f"packages.toml is not valid TOML: {exc}") from exc
    rows = data.get("packages", {})
    packages = {name: _package(name, row) for name, row in rows.items()}
    for package in packages.values():
        for dep in package.depends:
            if dep not in packages:
                raise PmError(f"packages.toml: {package.name} depends on unknown {dep!r}")
    return packages


@functools.cache
def definitions() -> Mapping[str, Package]:
    """Our package list (``packages.toml`` shipped beside this module), parsed once."""
    text = resources.files("indus.pm").joinpath("packages.toml").read_text(encoding="utf-8")
    return parse_definitions(text)


# -- target ----------------------------------------------------------------------------------------


def _musl(exists: Callable[[str], bool]) -> bool:
    return any(exists(p) for p in MUSL_LOADERS)


def current_target(
    env: Mapping[str, str], *, exists: Callable[[str], bool] = os.path.exists
) -> str:
    """This machine's lock target (``linux-x64``, ``darwin-arm64``, ...)."""
    from indus.platform import facts

    family = facts.os_family()
    arch = {"amd64": "x64", "arm64": "arm64"}.get(facts.process_arch())
    if arch is None or family not in ("linux", "darwin", "win32"):
        raise PmError(f"pm has no packages for {family}/{facts.process_arch()}")
    if family == "linux":
        if facts.is_termux(env):
            if arch != "arm64":
                raise PmError("pm supports Termux on arm64 only")
            return "linux-arm64-bionic"
        if _musl(exists):
            return f"linux-{arch}-musl"
    return f"{family}-{arch}"


# -- runner ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Runner:
    """An installed package: its executable, entry and the folders it puts first on ``PATH``
    (its own, then its dependencies')."""

    name: str
    version: str
    entry: Path
    executable: Path | None
    path_dirs: tuple[Path, ...]

    def path_value(self, base: str) -> str:
        own = [str(p) for p in self.path_dirs]
        return os.pathsep.join([*own, base] if base else own)

    def env(self, scope: Scope, builder: EnvBuilder) -> dict[str, str]:
        """The child environment: the builder's (R02.22) with the package folders first."""
        return builder(scope, extra={"PATH": self.path_value(scope.env.get("PATH", ""))})

    def argv(self, *args: str) -> list[str]:
        if self.executable is None:
            raise PmError(f"{self.name} has no executable")
        return [str(self.executable), *args]

    def run(
        self,
        args: Sequence[str],
        *,
        scope: Scope,
        builder: EnvBuilder,
        timeout: float,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            self.argv(*args),
            env=self.env(scope, builder),
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            check=False,
        )


# -- the manager -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    base: Path

    @property
    def lock(self) -> Path:
        return self.base / "lock.json"

    @property
    def facts(self) -> Path:
        return self.base / "facts.json"

    @property
    def store(self) -> Path:
        return self.base / "store"

    @property
    def downloads(self) -> Path:
        return self.base / "downloads"

    @property
    def install_lock(self) -> Path:
        return self.base / ".pm.lock"

    @property
    def receipt(self) -> Path:
        return self.base / "receipt.json"

    def install_lock_ready(self) -> Path:
        """The install lock's path, its folder created."""
        self.base.mkdir(parents=True, exist_ok=True)
        return self.install_lock


def _packaged_lock() -> Path | None:
    candidate = resources.files("indus.pm").joinpath("lock.json")
    return Path(str(candidate)) if candidate.is_file() else None


@dataclass
class Pm:
    root: Path
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    target: str | None = None
    packages: Mapping[str, Package] | None = None
    mirror: str = ""
    timeout: float = 60.0
    local_dirs: tuple[Path, ...] = ()
    transport: httpx.BaseTransport | None = None
    clock: Clock = field(default_factory=SystemClock)

    def __post_init__(self) -> None:
        self.paths = Paths(self.root / "pm")
        self.store = Store(self.paths.store)
        self._locks: StatCache[Lock] = StatCache(Lock.read)
        self._facts: StatCache[Facts] = StatCache(Facts.read)
        self._fetcher: Fetcher | None = None

    @classmethod
    def for_scope(cls, scope: Scope, **overrides: Any) -> Pm:
        """The root's pm with ``pm.mirror_url`` and ``pm.timeout_s`` from the profile settings."""
        from indus.foundation import config

        view = config.load(scope)
        mirror = view.get("pm.mirror_url")
        timeout = view.get("pm.timeout_s")
        settings: dict[str, Any] = {
            "env": scope.env,
            "mirror": mirror if isinstance(mirror, str) else "",
            "timeout": float(timeout) if isinstance(timeout, int | float) and timeout > 0 else 60.0,
            "clock": scope.clock,
        }
        settings.update(overrides)
        return cls(scope.root, **settings)

    # -- reading ----------------------------------------------------------------------------------

    @property
    def definitions(self) -> Mapping[str, Package]:
        return self.packages if self.packages is not None else definitions()

    def package(self, name: str) -> Package:
        found = self.definitions.get(name)
        if found is None:
            known = ", ".join(sorted(self.definitions))
            raise PmError(f"unknown package {name!r} (known: {known})")
        return found

    def machine_target(self) -> str:
        if self.target is None:
            self.target = current_target(self.env)
        return self.target

    def lock_path(self) -> Path:
        """The root's lock, else a lock shipped with the build, else the root's (empty)."""
        if os.path.exists(self.paths.lock):
            return self.paths.lock
        return _packaged_lock() or self.paths.lock

    def lock(self) -> Lock:
        return self._locks.get(self.lock_path())

    def facts(self) -> Facts:
        return self._facts.get(self.paths.facts)

    def fresh_facts(self) -> Facts:
        self._facts.forget(self.paths.facts)
        return self.facts()

    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            from indus.pm.fetch import Fetcher

            self._fetcher = Fetcher(
                self.paths.downloads,
                mirror=self.mirror,
                local_dirs=self.local_dirs,
                timeout=self.timeout,
                transport=self.transport,
            )
        return self._fetcher

    def close(self) -> None:
        if self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None

    def chain(self, name: str) -> list[Package]:
        """``name`` and everything it depends on, dependencies first."""
        order: list[Package] = []
        seen: set[str] = set()

        def visit(current: str, stack: tuple[str, ...]) -> None:
            if current in stack:
                raise PmError(f"dependency cycle: {' -> '.join((*stack, current))}")
            if current in seen:
                return
            package = self.package(current)
            for dep in package.depends:
                visit(dep, (*stack, current))
            seen.add(current)
            order.append(package)

        visit(name, ())
        return order

    def pin(self, name: str) -> Pin:
        found = self.lock().packages.get(name)
        if found is None:
            raise PmError(f"{name} is not in the lock", hint="run `indus pm lock`")
        return found

    def satisfied(self, name: str, facts: Facts | None = None) -> Fact | None:
        """The fact when it matches the lock for this machine and its entry folder exists."""
        pin = self.lock().packages.get(name)
        target = self.machine_target()
        artifact = pin.artifact(target) if pin is not None else None
        fact = (facts or self.facts()).packages.get(name)
        if pin is None or artifact is None or fact is None:
            return None
        if (fact.version, fact.sha256, fact.target) != (pin.version, artifact.sha256, target):
            return None
        return fact if self.store.entry(fact.entry).is_dir() else None

    def _runner(self, name: str, facts: Facts) -> Runner:
        dirs: list[Path] = []
        main: Runner | None = None
        for package in reversed(self.chain(name)):
            fact = facts.packages.get(package.name)
            if fact is None:
                raise PmError(f"{package.name} is not installed", hint="run `indus pm install`")
            entry = self.store.entry(fact.entry)
            dirs.extend(entry / d if d != "." else entry for d in fact.path)
            if package.name == name:
                exe = entry / fact.exe if fact.exe else None
                main = Runner(name, fact.version, entry, exe, ())
        assert main is not None
        unique = tuple(dict.fromkeys(dirs))
        return Runner(main.name, main.version, main.entry, main.executable, unique)

    def runner(self, name: str) -> Runner | None:
        """The installed package's runner without installing anything (None when not installed)."""
        facts = self.facts()
        try:
            if any(self.satisfied(p.name, facts) is None for p in self.chain(name)):
                return None
            return self._runner(name, facts)
        except PmError:
            return None

    # -- installing -------------------------------------------------------------------------------

    def ensure(self, name: str) -> Runner:
        """Install ``name`` (and its dependencies) as the lock says; return its runner."""
        chain = self.chain(name)
        facts = self.facts()
        if all(self.satisfied(p.name, facts) is not None for p in chain):
            return self._runner(name, facts)
        with FileLock(self.paths.install_lock_ready(), timeout=INSTALL_LOCK_TIMEOUT_S):
            facts = self.fresh_facts()
            for package in chain:
                if self.satisfied(package.name, facts) is None:
                    self._install(package, facts)
            return self._runner(name, facts)

    def install(self, name: str, *, force: bool = False) -> Fact:
        """Install one package (no dependency walk) under the install lock; ``force`` re-realises
        it even when the facts already match."""
        package = self.package(name)
        with FileLock(self.paths.install_lock_ready(), timeout=INSTALL_LOCK_TIMEOUT_S):
            facts = self.fresh_facts()
            current = self.satisfied(name, facts)
            if current is not None and not force:
                return current
            return self._install(package, facts)

    def realise(
        self, package: Package, version: str, url: str, sha256: str, target: str
    ) -> tuple[str, str]:
        """Fetch, unpack and publish one archive; return ``(entry name, tree digest)``."""
        blob = self.fetcher().fetch(url, sha256)
        name = entry_name(package.name, version, sha256)
        existing = self.store.entry(name)
        exe = package.exe_for(version, target)
        with self.store.staging() as staged:
            unpack(
                blob,
                staged,
                package.format_for(target),
                strip=package.strip_for(target),
                file_name=Path(exe).name if exe else package.name,
            )
            if exe is not None:
                binary = staged / exe
                if not binary.is_file():
                    raise PmError(f"{package.name}: the archive has no {exe}")
                if not target.startswith("win32"):
                    binary.chmod(binary.stat().st_mode | 0o111)
            digest = tree_digest(staged)
            if existing.exists() or existing.is_symlink():
                # A leftover entry for the same bytes (facts lost, or tampered): replaced whole.
                self.store.remove(existing)
            self.store.publish(staged, name)
        return name, digest

    def _install(self, package: Package, facts: Facts) -> Fact:
        pin = self.pin(package.name)
        target = self.machine_target()
        artifact = pin.artifact(target)
        if artifact is None:
            raise PmError(
                f"{package.name} {pin.version} has no artifact for {target}",
                hint="run `indus pm lock`",
            )
        name, digest = self.realise(package, pin.version, artifact.url, artifact.sha256, target)
        fact = Fact(
            version=pin.version,
            target=target,
            url=artifact.url,
            sha256=artifact.sha256,
            entry=name,
            digest=digest,
            exe=package.exe_for(pin.version, target),
            path=package.path_for(pin.version, target),
            installed_at=self.clock.now().isoformat(timespec="seconds"),
        )
        facts.packages[package.name] = fact
        self.save_facts(facts)
        return fact

    def save_facts(self, facts: Facts) -> None:
        facts.write(self.paths.facts)
        self._facts.put(self.paths.facts, facts)

    def exports(self, names: Sequence[str]) -> dict[str, list[str]]:
        """``{"PATH": [folders]}`` of the installed packages among ``names`` (no install)."""
        dirs: list[str] = []
        for name in names:
            runner = self.runner(name)
            if runner is not None:
                dirs.extend(str(p) for p in runner.path_dirs)
        return {"PATH": list(dict.fromkeys(dirs))} if dirs else {}
