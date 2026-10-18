"""The tool registry: every model-callable tool in the process, plus availability probes.

A tool is registered once with its name, the toolset it belongs to, a JSON schema and a handler.
Plugin tools go into a per-profile overlay, so a plugin loaded for one profile is invisible to the
others. Each change bumps ``generation``; the toolset resolver and the definitions memo key on it.

Availability probes take the ``Scope`` they are asked for and read secrets only through it
(``scope.secrets`` / ``scope.env``, R02.8). Their answers are cached per (probe, profile) for
``PROBE_TTL_S``; a probe that says "no" within ``PROBE_GRACE_S`` of its last "yes" is treated as a
flake and served as available without caching (R02.13); a probe that raises counts as unavailable;
work with no scope never runs a probe and is told "unavailable" without touching the cache
(R02.29).
"""

from __future__ import annotations

import copy
import inspect
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.errors import IndusError, ScopeUnresolved
from indus.foundation.scope import Scope, scope_or_none

log = logging.getLogger(__name__)

PROBE_TTL_S = 30.0
PROBE_GRACE_S = 60.0
PROBE_CACHE_MAX = 512

Probe = Callable[[Scope], bool]
SchemaOverrides = Callable[[Scope | None], Mapping[str, Any] | None]
# Gets the function dict and the names that survived filtering; returns a new dict or None (drop).
Rewrite = Callable[[dict[str, Any], frozenset[str]], dict[str, Any] | None]


class ToolError(IndusError):
    default_code = "tools.error"


class ToolSchemaInvalid(ToolError):
    default_code = "tools.bad_schema"


class ToolNameTaken(ToolError):
    default_code = "tools.name_taken"


@dataclass(frozen=True)
class CrossRef:
    """A sentence added to a tool's description only when ``tool`` survives filtering (R02.21)."""

    tool: str
    text: str


@dataclass(frozen=True)
class ToolTraits:
    """What the turn loop may assume about a tool (P04-14 planner, P04-18 guardrails).

    ``read_only``: no side effects, safe to run beside other admissible calls. ``path_arg``: the
    argument naming the one path the call touches (path-scoped file tools); ``path_default`` is
    used when the argument is absent. ``writer``: a path-scoped tool that changes that path.
    ``interactive``: waits on a person (never run beside others). ``mutating``: a success counts
    as progress for the loop guardrails.
    """

    read_only: bool = False
    path_arg: str | None = None
    path_default: str | None = None
    writer: bool = False
    interactive: bool = False
    mutating: bool = False


NO_TRAITS = ToolTraits()


@dataclass(frozen=True, eq=False)
class ToolEntry:
    name: str
    toolset: str
    schema: Mapping[str, Any]
    handler: Callable[..., Any]
    probe: Probe | None = None
    requires_secrets: tuple[str, ...] = ()
    is_async: bool = False
    emoji: str = ""
    max_result_chars: float | None = None
    schema_overrides: SchemaOverrides | None = None
    timeout_s: float | None = None
    cross_refs: tuple[CrossRef, ...] = ()
    rewrite: Rewrite | None = None
    profile: str | None = None
    traits: ToolTraits = NO_TRAITS

    @property
    def parameters(self) -> Mapping[str, Any] | None:
        params: Any = self.schema.get("parameters")
        return cast(Mapping[str, Any], params) if isinstance(params, Mapping) else None


def scope_key(scope: Scope) -> str:
    """The text that names a profile in caches and overlays: its home path."""
    return str(scope.home)


def _resolve(scope: Scope | None) -> Scope | None:
    return scope if scope is not None else scope_or_none()


# -- probe cache --------------------------------------------------------------------------------


def _probe_label(probe: Probe) -> str:
    return getattr(probe, "__qualname__", None) or repr(probe)


class ProbeCache:
    """Short-lived probe verdicts per (probe, profile) with flake grace."""

    def __init__(self, clock: Clock, *, max_entries: int = PROBE_CACHE_MAX) -> None:
        self._clock = clock
        self._max = max_entries
        self._verdicts: OrderedDict[tuple[Probe, str], tuple[float, bool]] = OrderedDict()
        self._last_true: OrderedDict[tuple[Probe, str], float] = OrderedDict()
        self._raise_warned: set[Probe] = set()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._verdicts)

    def clear(self) -> None:
        with self._lock:
            self._verdicts.clear()
            self._last_true.clear()
            self._raise_warned.clear()

    def _prune(self, now: float) -> None:
        for key in [k for k, (at, _) in self._verdicts.items() if now - at >= PROBE_TTL_S]:
            del self._verdicts[key]
        for key in [k for k, at in self._last_true.items() if now - at >= PROBE_GRACE_S]:
            del self._last_true[key]
        for table in (self._verdicts, self._last_true):
            while len(table) >= self._max:
                table.popitem(last=False)

    def _store(self, key: tuple[Probe, str], now: float, verdict: bool) -> None:
        self._verdicts.pop(key, None)
        self._verdicts[key] = (now, verdict)
        if verdict:
            self._last_true.pop(key, None)
            self._last_true[key] = now

    def check(self, probe: Probe, scope: Scope | None) -> bool:
        if scope is None:
            log.debug(
                "probe %s skipped: no profile scope here, tool unavailable", _probe_label(probe)
            )
            return False
        key = (probe, scope_key(scope))
        now = self._clock.monotonic()
        with self._lock:
            self._prune(now)
            hit = self._verdicts.get(key)
            if hit is not None:
                return hit[1]
        failure: BaseException | None = None
        try:
            verdict = bool(probe(scope))
        except ScopeUnresolved:
            log.warning(
                "probe %s lost its profile scope on the way to a secret; tool unavailable",
                _probe_label(probe),
            )
            return False
        except Exception as exc:
            verdict, failure = False, exc
        with self._lock:
            self._prune(now)
            if verdict:
                self._store(key, now, True)
                return True
            last = self._last_true.get(key)
            if last is not None and now - last < PROBE_GRACE_S:
                log.warning(
                    "probe %s said no %.0f s after saying yes; treated as a flake, tool kept",
                    _probe_label(probe),
                    now - last,
                )
                return True
            if failure is not None and probe not in self._raise_warned:
                self._raise_warned.add(probe)
                log.warning(
                    "probe %s raised %s; its tools are unavailable",
                    _probe_label(probe),
                    type(failure).__name__,
                    exc_info=failure,
                )
            elif failure is not None:
                log.debug("probe %s raised again: %s", _probe_label(probe), type(failure).__name__)
            else:
                log.info("probe %s said no; its tools are unavailable", _probe_label(probe))
            self._store(key, now, False)
            return False


# -- registry -----------------------------------------------------------------------------------


def _check_schema(name: object, schema: object) -> Mapping[str, Any]:
    if not isinstance(name, str) or not name:
        raise ToolSchemaInvalid(f"tool name must be a non-empty string, got {name!r}")
    if not isinstance(schema, Mapping):
        raise ToolSchemaInvalid(
            f"tool {name!r}: schema must be a JSON object (dict), got {type(schema).__name__}"
        )
    checked = cast(Mapping[str, Any], schema)
    params: Any = checked.get("parameters")
    if params is not None and not isinstance(params, Mapping):
        raise ToolSchemaInvalid(
            f"tool {name!r}: schema parameters must be a JSON object (dict), "
            f"got {type(params).__name__}"
        )
    return dict(checked)


class ToolRegistry:
    """Global registrations plus one overlay per profile key (see ``scope_key``)."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._global: dict[str, ToolEntry] = {}
        self._overlays: dict[str, dict[str, ToolEntry]] = {}
        self._lock = threading.RLock()
        self._generation = 0
        self.probes = ProbeCache(clock if clock is not None else SystemClock())

    @property
    def generation(self) -> int:
        return self._generation

    # -- writes ---------------------------------------------------------------------------------

    def register(
        self,
        name: str,
        toolset: str,
        schema: Mapping[str, Any],
        handler: Callable[..., Any],
        *,
        probe: Probe | None = None,
        requires_secrets: tuple[str, ...] | list[str] = (),
        is_async: bool | None = None,
        emoji: str = "",
        max_result_chars: float | None = None,
        schema_overrides: SchemaOverrides | None = None,
        timeout_s: float | None = None,
        cross_refs: tuple[CrossRef, ...] = (),
        rewrite: Rewrite | None = None,
        override: bool = False,
        profile: str | None = None,
        traits: ToolTraits = NO_TRAITS,
    ) -> ToolEntry:
        """Add or replace a tool. ``profile`` (a ``scope_key``) puts it in that profile's overlay.

        Same name and same toolset replaces silently (a reconnecting server re-registers). A name
        held by another toolset, or an overlay entry that would hide a global tool, needs
        ``override=True``.
        """
        clean = copy.deepcopy(_check_schema(name, schema))
        if not toolset:
            raise ToolSchemaInvalid(f"tool {name!r}: toolset must be a non-empty string")
        entry = ToolEntry(
            name=name,
            toolset=toolset,
            schema=clean,
            handler=handler,
            probe=probe,
            requires_secrets=tuple(requires_secrets),
            is_async=inspect.iscoroutinefunction(handler) if is_async is None else is_async,
            emoji=emoji,
            max_result_chars=max_result_chars,
            schema_overrides=schema_overrides,
            timeout_s=timeout_s,
            cross_refs=tuple(cross_refs),
            rewrite=rewrite,
            profile=profile,
            traits=traits,
        )
        with self._lock:
            slot = self._global if profile is None else self._overlays.setdefault(profile, {})
            current = slot.get(name)
            if current is None and profile is not None:
                hidden = self._global.get(name)
                if hidden is not None and not override:
                    self._drop_empty_overlay(profile)
                    raise ToolNameTaken(
                        f"tool {name!r} is a global tool (toolset {hidden.toolset!r}); "
                        "a profile overlay may replace it only with override=True"
                    )
                current = hidden
            if current is not None and current.toolset != toolset:
                if not override:
                    self._drop_empty_overlay(profile)
                    raise ToolNameTaken(
                        f"tool {name!r} already belongs to toolset {current.toolset!r}; "
                        f"registering it under {toolset!r} needs override=True"
                    )
                log.info(
                    "tool %s moves from toolset %s to %s (override)", name, current.toolset, toolset
                )
            slot[name] = entry
            self._generation += 1
        return entry

    def _drop_empty_overlay(self, profile: str | None) -> None:
        if profile is not None and not self._overlays.get(profile):
            self._overlays.pop(profile, None)

    def deregister(self, name: str, *, profile: str | None = None) -> bool:
        with self._lock:
            slot = self._global if profile is None else self._overlays.get(profile, {})
            if slot.pop(name, None) is None:
                return False
            self._drop_empty_overlay(profile)
            self._generation += 1
            return True

    # -- reads ----------------------------------------------------------------------------------

    def _merged(self, scope: Scope | None) -> dict[str, ToolEntry]:
        merged = dict(self._global)
        if scope is not None:
            merged.update(self._overlays.get(scope_key(scope), {}))
        return merged

    def get(self, name: str, scope: Scope | None = None) -> ToolEntry | None:
        """The profile's overlay entry first, then the global one."""
        resolved = _resolve(scope)
        with self._lock:
            if resolved is not None:
                found = self._overlays.get(scope_key(resolved), {}).get(name)
                if found is not None:
                    return found
            return self._global.get(name)

    def entries(self, scope: Scope | None = None) -> list[ToolEntry]:
        with self._lock:
            merged = self._merged(_resolve(scope))
        return [merged[n] for n in sorted(merged)]

    def names(self, scope: Scope | None = None) -> list[str]:
        return [e.name for e in self.entries(scope)]

    def names_in_toolset(self, toolset: str, scope: Scope | None = None) -> list[str]:
        return [e.name for e in self.entries(scope) if e.toolset == toolset]

    def toolset_names(self, scope: Scope | None = None) -> list[str]:
        return sorted({e.toolset for e in self.entries(scope)})

    # -- availability ---------------------------------------------------------------------------

    def available(
        self,
        entry: ToolEntry,
        scope: Scope | None = None,
        memo: dict[Probe, bool] | None = None,
    ) -> bool:
        """True when ``entry`` has no probe or its probe says yes for this profile.

        ``memo`` is one pass's table, so each distinct probe runs at most once per pass.
        """
        if entry.probe is None:
            return True
        if memo is not None and entry.probe in memo:
            return memo[entry.probe]
        verdict = self.probes.check(entry.probe, _resolve(scope))
        if memo is not None:
            memo[entry.probe] = verdict
        return verdict

    def invalidate_probes(self) -> None:
        """Forget every cached probe verdict (after a config or key change)."""
        self.probes.clear()
