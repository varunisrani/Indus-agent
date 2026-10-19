# pyright: strict
"""Loading the plugins discovery allowed (P13-15).

For each plugin, in dependency order: check its ``requires_indus`` range, then import its module
and call ``register(ctx)`` on a worker of the ``plugin-load`` pool with a deadline
(``plugins.load_timeout_seconds``: 10 s; 0 = none; 600 at most). A plugin that overruns, raises
(``SystemExit`` included) or has no ``register`` is skipped with a reason, whatever it registered
is taken back out, and the next plugin loads. An overrunning worker is abandoned: its context is
closed so nothing it registers later is kept, and it holds its pool worker until it returns; with
every worker stuck, further loads are refused rather than run on the caller.

``requires_plugins`` orders the load (dependencies first, ties by key). A requirement that is
missing, of the wrong version, failed to load or sits in a cycle skips its dependents.

Each loaded plugin gets an activation summary: what is live at once (commands, hooks, providers,
events, skills — skills reach open sessions through the P09 delta note) and what waits for the
next session (tools, prompt sections).
"""

from __future__ import annotations

import asyncio
import graphlib
import hashlib
import importlib.metadata
import importlib.util
import inspect
import logging
import sys
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from indus import __version__
from indus.foundation.blocking import BlockingError, BlockingPool, DeadlineMissed, run_blocking
from indus.foundation.clock import Clock
from indus.plugins.context import PluginContext, PluginError, PluginHost, Provided
from indus.plugins.discovery import ENTRY_POINT_GROUP, Discovery, EntryPointLike, PluginRecord
from indus.plugins.manifest import PluginManifest, satisfies, version_gate
from indus.plugins.settings import bounded_seconds, plugins_setting

__all__ = [
    "DEFAULT_LOAD_TIMEOUT_S",
    "LOAD_POOL",
    "MAX_LOAD_TIMEOUT_S",
    "MODULE_ROOT",
    "LoadOutcome",
    "LoadReport",
    "PluginLoadFailed",
    "PluginLoader",
    "activation_summary",
    "load_timeout",
]

log = logging.getLogger(__name__)

DEFAULT_LOAD_TIMEOUT_S = 10.0
MAX_LOAD_TIMEOUT_S = 600.0
MODULE_ROOT = "indus_plugins"
# Stuck loaders keep their worker: eight of them and further loads are refused.
LOAD_POOL = BlockingPool("plugin-load", 8)


class PluginLoadFailed(PluginError):
    default_code = "plugins.load_failed"


def load_timeout(scope: Any) -> float:
    """``plugins.load_timeout_seconds`` (10 s default; 0 = no deadline; clamped to 600)."""
    return bounded_seconds(
        plugins_setting(scope, "load_timeout_seconds", None),
        name="plugins.load_timeout_seconds",
        default=DEFAULT_LOAD_TIMEOUT_S,
        maximum=MAX_LOAD_TIMEOUT_S,
    )


@dataclass(frozen=True)
class LoadOutcome:
    key: str
    loaded: bool
    reason: str = ""
    summary: Mapping[str, Mapping[str, list[str]]] = field(
        default_factory=dict[str, Mapping[str, list[str]]]
    )


@dataclass
class LoadReport:
    outcomes: list[LoadOutcome] = field(default_factory=list[LoadOutcome])

    def get(self, key: str) -> LoadOutcome | None:
        return next((o for o in self.outcomes if o.key == key), None)

    def loaded(self) -> list[str]:
        return [o.key for o in self.outcomes if o.loaded]

    def skipped(self) -> dict[str, str]:
        return {o.key: o.reason for o in self.outcomes if not o.loaded}

    def order(self) -> list[str]:
        return [o.key for o in self.outcomes]


def activation_summary(provided: Provided) -> dict[str, dict[str, list[str]]]:
    """``{live_now: {...}, next_session: {...}}`` with only the non-empty groups."""
    live = {
        "commands": list(provided.commands),
        "hooks": sorted(set(provided.hooks)),
        "providers": list(provided.providers),
        "events": list(provided.events),
        "skills": list(provided.skills),
        "cli_commands": list(provided.cli_commands),
    }
    later = {"tools": list(provided.tools), "prompt_sections": list(provided.prompt_sections)}
    return {
        "live_now": {k: v for k, v in live.items() if v},
        "next_session": {k: v for k, v in later.items() if v},
    }


# -- import -------------------------------------------------------------------------------------


def _evict(module_name: str) -> None:
    prefix = module_name + "."
    for name in list(sys.modules):
        if name == module_name or name.startswith(prefix):
            sys.modules.pop(name, None)


def _module_name(manifest: PluginManifest, profile: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in manifest.plugin_id)
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:8]
    return f"{MODULE_ROOT}.{slug}__{digest}"


def _import_folder(manifest: PluginManifest, module_name: str) -> types.ModuleType:
    folder = Path(manifest.path)
    init = folder / "__init__.py"
    if not init.is_file():
        raise PluginLoadFailed(f"{folder} has no __init__.py")
    if MODULE_ROOT not in sys.modules:
        root = types.ModuleType(MODULE_ROOT)
        root.__path__ = []
        sys.modules[MODULE_ROOT] = root
    _evict(module_name)
    spec = importlib.util.spec_from_file_location(
        module_name, init, submodule_search_locations=[str(folder)]
    )
    if spec is None or spec.loader is None:
        raise PluginLoadFailed(f"{init} cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        _evict(module_name)  # never leave a half-imported plugin behind
        raise
    return module


class PluginLoader:
    """Loads plugin records into one ``PluginHost``."""

    def __init__(
        self,
        host: PluginHost,
        *,
        clock: Clock | None = None,
        timeout_s: float | None = None,
        running_version: str | None = None,
        pool: BlockingPool | None = None,
        entry_points: Iterable[EntryPointLike] | None = None,
    ) -> None:
        self.host = host
        self.clock = clock if clock is not None else host.clock
        self.timeout_s = timeout_s if timeout_s is not None else load_timeout(host.scope)
        self.running_version = running_version or __version__
        self.pool = pool if pool is not None else LOAD_POOL
        self._entry_points = list(entry_points) if entry_points is not None else None

    # -- order ----------------------------------------------------------------------------------

    def _plan(
        self, records: Sequence[PluginRecord]
    ) -> tuple[list[PluginRecord], dict[str, list[tuple[str, str]]], dict[str, str]]:
        """(load order, requirements per key as (key, version range), skipped key → reason)."""
        by_key = {r.key: r for r in records}
        by_name: dict[str, str] = {}
        for record in sorted(records, key=lambda r: r.key):
            by_name.setdefault(record.name, record.key)
        needs: dict[str, list[tuple[str, str]]] = {}
        skipped: dict[str, str] = {}
        for record in records:
            manifest = cast(PluginManifest, record.manifest)
            wanted: list[tuple[str, str]] = []
            for req in manifest.requires_plugins:
                target = req.name if req.name in by_key else by_name.get(req.name)
                if target is None:
                    skipped[record.key] = (
                        f"needs plugin {req.name}, which is not installed or not enabled"
                    )
                elif target == record.key:
                    log.warning("plugin %s lists itself as a requirement; ignored", record.key)
                else:
                    wanted.append((target, req.version))
            needs[record.key] = wanted
        pending = {k: [t for t, _ in v] for k, v in needs.items() if k not in skipped}
        order: list[str] = []
        while True:
            sorter = graphlib.TopologicalSorter(
                {k: set(v) & set(pending) for k, v in pending.items()}
            )
            try:
                sorter.prepare()
            except graphlib.CycleError as exc:
                cycle = [str(k) for k in cast(Sequence[object], exc.args[1])]
                for key in set(cycle):
                    skipped[key] = "dependency cycle: " + " -> ".join(cycle)
                    pending.pop(key, None)
                continue
            while sorter.is_active():
                ready = sorted(cast(Iterable[str], sorter.get_ready()))
                order.extend(ready)
                sorter.done(*ready)
            break
        # Dependents of skipped plugins stay in the order; they are skipped when reached.
        ordered = [by_key[k] for k in order]
        ordered.extend(by_key[k] for k in sorted(skipped) if k in by_key)
        return ordered, needs, skipped

    # -- one plugin -----------------------------------------------------------------------------

    def _entry_point(self, manifest: PluginManifest) -> object:
        found = self._entry_points
        if found is None:
            found = list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))
        for ep in found:
            if ep.name == manifest.name:
                return cast(Any, ep).load()
        raise PluginLoadFailed(f"entry point {manifest.name!r} not found in {ENTRY_POINT_GROUP}")

    def _import_and_register(self, record: PluginRecord, ctx: PluginContext) -> None:
        """Runs on the load worker: the part the plugin controls."""
        manifest = cast(PluginManifest, record.manifest)
        module_name = _module_name(manifest, self.host.profile)
        try:
            if record.source == "entrypoint":
                target = self._entry_point(manifest)
            else:
                target = _import_folder(manifest, module_name)
            register = (
                getattr(target, "register", None)
                if isinstance(target, types.ModuleType)
                else target
            )
            if not callable(register):
                raise PluginLoadFailed("the plugin has no register(ctx) function")
            value = register(ctx)
            if inspect.isawaitable(value):
                awaited = value

                async def finish() -> object:
                    return await awaited

                asyncio.run(finish())
        except SystemExit as exc:  # a plugin's stray exit must not end the host
            _evict(module_name)
            raise PluginLoadFailed(f"SystemExit({exc.code!r}) during import or register()") from exc
        except BaseException:
            _evict(module_name)  # a failed plugin leaves no module for a later retry to reuse
            raise

    async def _load_one(self, record: PluginRecord) -> LoadOutcome:
        manifest = cast(PluginManifest, record.manifest)
        key = record.key
        refusal = version_gate(manifest, self.running_version)
        if refusal is not None:
            log.warning("plugin %s skipped: %s", key, refusal)
            return LoadOutcome(key, False, refusal)
        ctx = self.host.context(manifest)
        deadline = self.timeout_s if self.timeout_s > 0 else None
        try:
            await run_blocking(
                self._import_and_register,
                record,
                ctx,
                pool=self.pool,
                deadline=deadline,
                clock=self.clock,
                label=f"plugin load {key}",
            )
        except DeadlineMissed:
            ctx.close()
            self.host.undo(ctx.plugin_id)
            reason = f"load timed out after {deadline or 0.0:g} s"
            log.warning(
                "plugin %s skipped: %s (its import or register() never returned)", key, reason
            )
            return LoadOutcome(key, False, reason)
        except BlockingError as exc:
            ctx.close()
            self.host.undo(ctx.plugin_id)
            reason = (
                f"not loaded: {self.pool.workers} earlier plugin loads are still stuck; restart "
                f"Indus to retry ({exc.code})"
            )
            log.warning("plugin %s skipped: %s", key, reason)
            return LoadOutcome(key, False, reason)
        except Exception as exc:
            ctx.close()
            self.host.undo(ctx.plugin_id)
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("plugin %s failed to load: %s", key, reason, exc_info=exc)
            return LoadOutcome(key, False, reason)
        provided = self.host.provided.get(ctx.plugin_id, Provided())
        summary = activation_summary(provided)
        log.info("plugin %s loaded: %s", key, summary)
        return LoadOutcome(key, True, summary=summary)

    # -- all ------------------------------------------------------------------------------------

    async def load(self, plugins: Discovery | Sequence[PluginRecord]) -> LoadReport:
        """Load every loadable record; one outcome per record, in load order."""
        records = plugins.loadable() if isinstance(plugins, Discovery) else list(plugins)
        records = [r for r in records if r.manifest is not None]
        if self.host.loop is None:
            self.host.bind_loop()
        order, needs, skipped = self._plan(records)
        report = LoadReport()
        versions = {r.key: cast(PluginManifest, r.manifest).version for r in records}
        done: dict[str, bool] = {}
        for record in order:
            reason = skipped.get(record.key) or self._blocked_by(record.key, needs, done, versions)
            if reason is not None:
                log.warning("plugin %s skipped: %s", record.key, reason)
                outcome = LoadOutcome(record.key, False, reason)
            else:
                outcome = await self._load_one(record)
            done[record.key] = outcome.loaded
            report.outcomes.append(outcome)
        return report

    @staticmethod
    def _blocked_by(
        key: str,
        needs: Mapping[str, list[tuple[str, str]]],
        done: Mapping[str, bool],
        versions: Mapping[str, str],
    ) -> str | None:
        for target, spec in needs.get(key, []):
            if not done.get(target, False):
                return f"its requirement {target} did not load"
            found = versions.get(target, "")
            if spec and (not found or not satisfies(spec, found)):
                return f"needs {target} {spec}, found {found or 'no version'}"
        return None
