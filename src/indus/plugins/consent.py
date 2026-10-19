# pyright: strict
"""Asking the person before a plugin gets more than tools and hooks (P13-22).

Capabilities are consent and audit, not a sandbox: a plugin runs inside Indus with the person's
rights. What they record is which riskier surfaces the person agreed the plugin may use; the host
checks them through ``ctx.has_capability`` (P13-16).

- ``CAPABILITIES``: the ids a manifest may declare, each with a one-line risk note. Unknown ids
  are ignored and logged: nothing in Indus would honour a grant of them.
- Grants live in config: ``plugins.entries.<id>.granted_capabilities: [{name, granted_at}]`` and
  ``plugins.entries.<id>.capabilities_consent: {hash, declared, at}`` (``hash`` = sha256 of the
  sorted declared set the person was shown). The person is asked once per declared set: a refusal
  is recorded too, so it is not asked again until the plugin declares something new, and then only
  the new capabilities are asked about. Without an ``ask`` (no terminal) nothing is granted.
- Python dependencies are a separate question: every entry must be pinned (``name==version``);
  they install with ``uv pip install`` into this interpreter's environment.
- ``requires_env``: missing names are asked for and stored in the profile's secret store.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.plugins.context import PluginError
from indus.plugins.install import run_process
from indus.plugins.manifest import EnvRequirement, PluginManifest
from indus.safety.child_env import child_env

__all__ = [
    "CAPABILITIES",
    "CONSENT_KEY",
    "GRANTED_KEY",
    "CapabilityConsent",
    "ConsentError",
    "capability_text",
    "consent_capabilities",
    "declared_capabilities",
    "declared_hash",
    "granted_capabilities",
    "install_dependencies",
    "is_memory_provider",
    "missing_env",
    "new_capabilities",
    "pinned_requirements",
    "revoke_capabilities",
    "set_grants",
]

log = logging.getLogger(__name__)

CAPABILITIES: Mapping[str, str] = {
    "tools.override": "replace a built-in tool; every call to it then runs the plugin's code",
    "llm.provider": "send host model calls to another provider, with your credentials",
    "llm.model": "choose which model host model calls use (and so what they cost)",
    "llm.agent": "attribute its model calls to another agent id",
    "llm.profile": "run its model calls under another profile's settings and keys",
    "llm.task": "use the host's auxiliary task lanes for its own model calls",
    "platform.actions": "act on connected chat platforms as the gateway's bot account",
}
GRANTED_KEY = "granted_capabilities"
CONSENT_KEY = "capabilities_consent"
DEPS_TIMEOUT_S = 600.0
_PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.+!_-]+$")


class ConsentError(PluginError):
    default_code = "plugins.consent"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# -- capabilities ---------------------------------------------------------------------------------


def declared_capabilities(manifest: PluginManifest) -> list[str]:
    """The known capabilities ``manifest`` declares, in order, without repeats."""
    found: list[str] = []
    for raw in manifest.capabilities:
        cap = raw.strip()
        if cap not in CAPABILITIES:
            log.warning(
                "plugin %s declares unknown capability %r; it cannot be granted (known: %s)",
                manifest.name,
                cap,
                ", ".join(sorted(CAPABILITIES)),
            )
        elif cap not in found:
            found.append(cap)
    return found


def declared_hash(capabilities: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(capabilities))).encode()).hexdigest()


def _entries(tree: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    plugins = tree.get("plugins")
    if not isinstance(plugins, MutableMapping):
        plugins = {}
        tree["plugins"] = plugins
    section = cast(MutableMapping[str, Any], plugins)
    entries = section.get("entries")
    if not isinstance(entries, MutableMapping):
        entries = {}
        section["entries"] = entries
    return cast(MutableMapping[str, Any], entries)


def _entry(tree: MutableMapping[str, Any], plugin_id: str) -> MutableMapping[str, Any]:
    entries = _entries(tree)
    entry = entries.get(plugin_id)
    if not isinstance(entry, MutableMapping):
        entry = {}
        entries[plugin_id] = entry
    return cast(MutableMapping[str, Any], entry)


def _read_entry(tree: Mapping[str, Any], plugin_id: str) -> Mapping[str, Any]:
    plugins = tree.get("plugins")
    if not isinstance(plugins, Mapping):
        return {}
    entries = cast(Mapping[str, Any], plugins).get("entries")
    if not isinstance(entries, Mapping):
        return {}
    entry = cast(Mapping[str, Any], entries).get(plugin_id)
    return cast(Mapping[str, Any], entry) if isinstance(entry, Mapping) else {}


def granted_capabilities(tree: Mapping[str, Any], plugin_id: str) -> list[str]:
    """Granted capability names (both the ``{name}`` and the plain-string forms)."""
    raw = _read_entry(tree, plugin_id).get(GRANTED_KEY)
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in cast(list[object], raw):
        name = cast(Mapping[str, Any], item).get("name") if isinstance(item, Mapping) else item
        if isinstance(name, str) and name in CAPABILITIES and name not in names:
            names.append(name)
    return names


def new_capabilities(tree: Mapping[str, Any], plugin_id: str, declared: Sequence[str]) -> list[str]:
    """Declared capabilities the person was not asked about yet (and has not granted)."""
    granted = set(granted_capabilities(tree, plugin_id))
    record = _read_entry(tree, plugin_id).get(CONSENT_KEY)
    shown: set[str] = set()
    if isinstance(record, Mapping):
        before = cast(Mapping[str, Any], record).get("declared")
        if isinstance(before, list):
            shown = {c for c in cast(list[object], before) if isinstance(c, str)}
    return [c for c in declared if c not in granted and c not in shown]


def set_grants(
    tree: MutableMapping[str, Any],
    plugin_id: str,
    grant: Iterable[str],
    declared: Sequence[str],
    *,
    now: str | None = None,
) -> list[str]:
    """Record a consent decision: ``grant`` added (earlier grants of still-declared or explicitly
    granted capabilities kept), and the declared set the person saw. Returns the granted names."""
    moment = now or _now()
    entry = _entry(tree, plugin_id)
    adding = [c for c in grant if c in CAPABILITIES]
    previous = entry.get(GRANTED_KEY)
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(previous, list):
        for item in cast(list[object], previous):
            name = cast(Mapping[str, Any], item).get("name") if isinstance(item, Mapping) else item
            if not isinstance(name, str) or name not in CAPABILITIES or name in seen:
                continue
            if name not in declared and name not in adding:
                continue
            at = (
                cast(Mapping[str, Any], item).get("granted_at")
                if isinstance(item, Mapping)
                else None
            )
            kept.append({"name": name, "granted_at": at if isinstance(at, str) else moment})
            seen.add(name)
    for name in adding:
        if name not in seen:
            kept.append({"name": name, "granted_at": moment})
            seen.add(name)
    entry[GRANTED_KEY] = kept
    entry[CONSENT_KEY] = {
        "hash": declared_hash(declared),
        "declared": sorted(set(declared)),
        "at": moment,
    }
    log.info(
        "plugin %s capability consent: granted %s of %s",
        plugin_id,
        ", ".join(sorted(seen)) or "none",
        ", ".join(sorted(set(declared))) or "none",
    )
    return [k["name"] for k in kept]


def revoke_capabilities(
    tree: MutableMapping[str, Any], plugin_id: str, names: Iterable[str]
) -> list[str]:
    """Remove grants; returns what was removed."""
    entry = _entry(tree, plugin_id)
    drop = set(names)
    raw = entry.get(GRANTED_KEY)
    if not isinstance(raw, list):
        return []
    kept: list[object] = []
    removed: list[str] = []
    for item in cast(list[object], raw):
        name: object = item
        if isinstance(item, Mapping):
            name = cast(Mapping[str, Any], item).get("name")
        if name in drop:
            removed.append(str(name))
        else:
            kept.append(cast(object, item))
    entry[GRANTED_KEY] = kept
    return removed


def capability_text(plugin: str, capabilities: Sequence[str]) -> str:
    lines = [f"Plugin {plugin} asks to:"]
    lines.extend(f"  - {cap}: {CAPABILITIES[cap]}" for cap in capabilities)
    lines.append(
        "This is a record of what you allow, not a sandbox: the plugin runs inside Indus with your "
        "rights. Refusing keeps the plugin on without these."
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class CapabilityConsent:
    granted: list[str] = field(default_factory=list[str])
    refused: list[str] = field(default_factory=list[str])
    waiting: list[str] = field(default_factory=list[str])
    asked: bool = False


def consent_capabilities(
    tree: MutableMapping[str, Any],
    manifest: PluginManifest,
    *,
    ask: Callable[[str], bool] | None,
    now: str | None = None,
) -> CapabilityConsent:
    """Ask once about the capabilities ``manifest`` declares that are new to the person.

    ``ask(text)`` shows the text and returns the answer; None (no terminal, unattended) grants
    nothing and records nothing, so the question comes again on a terminal.
    """
    plugin_id = manifest.plugin_id
    declared = declared_capabilities(manifest)
    fresh = new_capabilities(tree, plugin_id, declared)
    if not fresh:
        return CapabilityConsent(granted=granted_capabilities(tree, plugin_id))
    if ask is None:
        return CapabilityConsent(granted=granted_capabilities(tree, plugin_id), waiting=fresh)
    try:
        yes = bool(ask(capability_text(manifest.name, fresh)))
    except Exception:
        log.warning("the capability question failed; nothing granted", exc_info=True)
        yes = False
    granted = set_grants(tree, plugin_id, fresh if yes else [], declared, now=now)
    return CapabilityConsent(granted=granted, refused=[] if yes else fresh, asked=True)


# -- dependencies ---------------------------------------------------------------------------------


def pinned_requirements(manifest: PluginManifest) -> list[str]:
    """The manifest's Python dependencies; every one must be pinned exactly (``name==version``)."""
    loose = [d for d in manifest.python_dependencies if not _PINNED.match(d.strip())]
    if loose:
        raise ConsentError(
            f"plugin {manifest.name} lists dependencies that are not pinned exactly: "
            + ", ".join(loose),
            hint="each entry must look like name==1.2.3",
        )
    return [d.strip() for d in manifest.python_dependencies]


DepsRunner = Callable[[Sequence[str], Path, Mapping[str, str], float], tuple[int, str]]


def _run_deps(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
) -> tuple[int, str]:
    result = run_process(argv, cwd, env, timeout, None)
    return result.code, (result.stderr or result.stdout)[-2000:]


def install_dependencies(
    scope: Scope, requirements: Sequence[str], *, runner: DepsRunner | None = None
) -> tuple[bool, str]:
    """``uv pip install`` the pinned ``requirements`` into this interpreter; (ok, output tail)."""
    if not requirements:
        return True, ""
    uv = which("uv", scope.env.get("PATH"))
    if not uv:
        return False, "uv is not on PATH, so the dependencies were not installed"
    argv = [uv, "pip", "install", "--python", sys.executable, *requirements]
    run = runner if runner is not None else _run_deps
    try:
        code, output = run(argv, scope.home, child_env(scope), DEPS_TIMEOUT_S)
    except PluginError as exc:
        return False, str(exc)
    return code == 0, output


# -- env and kinds --------------------------------------------------------------------------------


def missing_env(scope: Scope, manifest: PluginManifest) -> list[EnvRequirement]:
    """``requires_env`` entries set neither in the scope env nor in the secret store."""
    missing: list[EnvRequirement] = []
    for item in manifest.requires_env:
        need = item if isinstance(item, EnvRequirement) else EnvRequirement(name=item)
        if scope.env.get(need.name):
            continue
        store = scope.secrets
        value: str | None = None
        if store is not None:
            try:
                value = store.get(need.name)
            except Exception:
                value = None
        if not value:
            missing.append(need)
    return missing


def is_memory_provider(manifest: PluginManifest) -> bool:
    return manifest.category == "memory" or manifest.kind == "exclusive"
