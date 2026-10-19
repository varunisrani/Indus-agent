"""Toolsets: named bundles of tools, and the resolver that turns a selection into tool names.

A ``Toolset`` lists tools and may include other toolsets. Resolution is recursive; a cycle or a
diamond is harmless (a toolset met again on the same walk adds nothing). Tools a plugin or server
registered into a toolset are merged into that toolset's members, and a toolset known only to the
registry resolves to its registered tools (R02.25, D-16.74).

Platform bundles (``indus-*``) all start from one shared ``core`` list, which includes the named
sets its tools come from, so a tool registered into ``file`` reaches every bundle that carries
``core``. ``indus-webhook`` is a fixed floor (read-only web, vision, clarify; R02.27) and takes
nothing from the registry. No toolset contains a tool that sends messages for the user (R02.26).

``select`` applies a session's choice: enabled toolsets (``None`` = all), then platform gating, then
disabled toolsets, always last, so no composite can bring a disabled tool back. Disabling a bundle
or a posture removes only what it adds on top of ``core``. Postures (``coding``) are per session:
``persistable`` drops them from any selection that is about to be written to config.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass

from indus.foundation.scope import Scope, scope_or_none
from indus.tools.registry import ToolRegistry, scope_key

log = logging.getLogger(__name__)

EVERYTHING = frozenset({"all", "*"})
BUNDLE_PREFIX = "indus-"
CORE = "core"
MEMO_MAX = 256

# The tools every platform bundle starts from. Later phases append their tools here (and to the
# named sets below) when they land.
CORE_TOOLS: tuple[str, ...] = (
    "terminal",
    "process_manage",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "web_search",
    "web_extract",
    "todo_list",
    "clarify",
    "memory",
    "session_search",
    "skills_list",
    "skill_view",
    "skill_manage",
    "execute_code",
    "delegate_task",
)
# Left out of the coding posture once those tools exist.
NOT_FOR_CODING = frozenset({"image_generate", "text_to_speech", "cronjob_manage", "computer_use"})
# Read-only research and a question back to the sender: the whole webhook floor.
WEBHOOK_FLOOR: tuple[str, ...] = ("web_search", "web_extract", "vision_analyze", "clarify")


@dataclass(frozen=True)
class Toolset:
    description: str
    tools: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()
    platforms: frozenset[str] | None = None  # set: only sessions on these platforms get it
    posture: bool = False  # per-session posture; never written to config
    registry_members: bool = True  # False: registered tools are not merged in (fixed floors)


def _set(description: str, *tools: str, includes: tuple[str, ...] = ()) -> Toolset:
    return Toolset(description, tuple(tools), includes)


_NAMED: dict[str, Toolset] = {
    "terminal": _set(
        "Run shell commands and manage background processes", "terminal", "process_manage"
    ),
    "file": _set(
        "Read, write, patch and search files", "read_file", "write_file", "patch", "search_files"
    ),
    "web": _set("Search the web and extract page content", "web_search", "web_extract"),
    "search": _set("Web search without page extraction", "web_search"),
    "vision": _set("Look at and describe images", "vision_analyze"),
    "todo": _set("Plan and track multi-step work in the session", "todo_list"),
    "clarify": _set("Ask the user a question and wait for the answer", "clarify"),
    "memory": _set("Save short facts that every later session should know", "memory"),
    "session_search": _set("Look up earlier conversations of this profile", "session_search"),
    "skills": _set(
        "Find, open and improve saved skills (tested procedures)",
        "skills_list",
        "skill_view",
        "skill_manage",
    ),
    "code_execution": _set("Run Python that calls the file, web and shell tools", "execute_code"),
    "delegation": _set("Hand tasks to subagents and control them", "delegate_task"),
}


def _member_sets(tools: Iterable[str]) -> tuple[str, ...]:
    """The named sets whose every tool is in ``tools`` (their registered members come along)."""
    have = set(tools)
    return tuple(n for n, ts in _NAMED.items() if ts.tools and set(ts.tools) <= have)


def _bundle(description: str, tools: Iterable[str], *, posture: bool = False) -> Toolset:
    listed = tuple(tools)
    return Toolset(description, listed, _member_sets(listed), posture=posture)


def _core_minus(*left_out: str) -> tuple[str, ...]:
    return tuple(t for t in CORE_TOOLS if t not in left_out)


_CODING = tuple(t for t in CORE_TOOLS if t not in NOT_FOR_CODING)

TOOLSETS: dict[str, Toolset] = {
    **_NAMED,
    CORE: _bundle("The shared core list every platform bundle starts from", CORE_TOOLS),
    "debugging": _set(
        "Troubleshooting kit: shell, processes, files and the web",
        "terminal",
        "process_manage",
        includes=("web", "file"),
    ),
    "safe": _set("Research without shell access or file writes", includes=("web", "vision")),
    "coding": _bundle("Working on code: files, shell, web docs, planning", _CODING, posture=True),
    "indus-cli": Toolset("Interactive command line", includes=(CORE,)),
    "indus-cron": Toolset("Scheduled jobs", includes=(CORE,)),
    "indus-acp": _bundle(
        "Editor integration: the coding posture without interactive questions",
        (t for t in _CODING if t != "clarify"),
    ),
    "indus-api-server": _bundle(
        "HTTP API server: core tools without interactive or audio tools",
        _core_minus("text_to_speech", "clarify", "computer_use"),
    ),
    "indus-webhook": Toolset(
        "Incoming webhooks: untrusted payloads get read-only tools only",
        WEBHOOK_FLOOR,
        registry_members=False,
    ),
    "indus-gateway": Toolset(
        "Messaging gateway: the union of the platform bundles it serves",
        includes=(CORE, "indus-webhook"),
    ),
}


# -- lookup -------------------------------------------------------------------------------------


def _scope(scope: Scope | None) -> Scope | None:
    return scope if scope is not None else scope_or_none()


def toolset_names(registry: ToolRegistry | None = None, scope: Scope | None = None) -> list[str]:
    names = set(TOOLSETS)
    if registry is not None:
        names.update(registry.toolset_names(_scope(scope)))
    return sorted(names)


def is_known(name: str, registry: ToolRegistry | None = None, scope: Scope | None = None) -> bool:
    return name in EVERYTHING or name in toolset_names(registry, scope)


def get_toolset(
    name: str, registry: ToolRegistry | None = None, scope: Scope | None = None
) -> Toolset | None:
    """The static entry with registered members merged in, or a registry-only toolset."""
    static = TOOLSETS.get(name)
    registered = registry.names_in_toolset(name, _scope(scope)) if registry is not None else []
    if static is None:
        if not registered:
            return None
        return Toolset(f"Registered toolset {name}", tuple(registered))
    if not registered or not static.registry_members:
        return static
    merged = tuple(sorted(set(static.tools) | set(registered)))
    return Toolset(
        static.description,
        merged,
        static.includes,
        static.platforms,
        static.posture,
        static.registry_members,
    )


# -- resolution ---------------------------------------------------------------------------------

_memo: dict[tuple[str, int, int, str | None], tuple[str, ...]] = {}
_memo_lock = threading.Lock()


def clear_memo() -> None:
    with _memo_lock:
        _memo.clear()


def _walk(
    name: str, registry: ToolRegistry | None, scope: Scope | None, seen: set[str]
) -> set[str]:
    if name in seen:
        return set()  # cycle or diamond: these tools arrive by the other path
    seen.add(name)
    toolset = get_toolset(name, registry, scope)
    if toolset is None:
        return set()
    found = set(toolset.tools)
    for inner in toolset.includes:
        found |= _walk(inner, registry, scope, seen)
    return found


def _gated(registry: ToolRegistry | None, scope: Scope | None) -> set[str]:
    return {
        n
        for n in toolset_names(registry, scope)
        if (ts := TOOLSETS.get(n)) is not None and ts.platforms is not None
    }


def resolve(
    name: str, registry: ToolRegistry | None = None, scope: Scope | None = None
) -> list[str]:
    """Sorted tool names of ``name`` and everything it includes; unknown → ``[]``."""
    resolved_scope = _scope(scope)
    key = (
        name,
        id(registry) if registry is not None else 0,
        registry.generation if registry is not None else 0,
        scope_key(resolved_scope) if resolved_scope is not None else None,
    )
    with _memo_lock:
        hit = _memo.get(key)
    if hit is not None:
        return list(hit)
    if name in EVERYTHING:
        found: set[str] = set()
        gated = _gated(registry, resolved_scope)
        for each in toolset_names(registry, resolved_scope):
            if each not in gated:
                found |= _walk(each, registry, resolved_scope, set())
    else:
        found = _walk(name, registry, resolved_scope, set())
    result = tuple(sorted(found))
    with _memo_lock:
        if len(_memo) >= MEMO_MAX:
            _memo.clear()  # entries of older generations are never asked for again
        _memo[key] = result
    return list(result)


def _adds_on_top_of_core(name: str, registry: ToolRegistry | None, scope: Scope | None) -> set[str]:
    return set(resolve(name, registry, scope)) - set(resolve(CORE, registry, scope))


@dataclass(frozen=True)
class Selection:
    tools: frozenset[str]
    unknown: tuple[str, ...] = ()


def select(
    enabled: Iterable[str] | None,
    disabled: Iterable[str] | None = None,
    *,
    registry: ToolRegistry | None = None,
    scope: Scope | None = None,
    platform: str | None = None,
) -> Selection:
    """The tool names a session gets. ``enabled=None`` means every non-gated toolset."""
    resolved_scope = _scope(scope)
    unknown: list[str] = []
    tools: set[str] = set()
    for name in ["all"] if enabled is None else list(enabled):
        if not is_known(name, registry, resolved_scope):
            unknown.append(name)
            continue
        tools |= set(resolve(name, registry, resolved_scope))
    for name in _gated(registry, resolved_scope):
        allowed = TOOLSETS[name].platforms or frozenset()
        if platform not in allowed:
            tools -= set(resolve(name, registry, resolved_scope))
    for name in disabled or ():
        if not is_known(name, registry, resolved_scope):
            unknown.append(name)
            continue
        static = TOOLSETS.get(name)
        if name.startswith(BUNDLE_PREFIX) or (static is not None and static.posture):
            tools -= _adds_on_top_of_core(name, registry, resolved_scope)
        else:
            tools -= set(resolve(name, registry, resolved_scope))
    if unknown:
        log.warning("unknown toolset name(s) ignored: %s", ", ".join(unknown))
    return Selection(frozenset(tools), tuple(unknown))


def persistable(names: Iterable[str]) -> list[str]:
    """``names`` without posture toolsets: what may be written to a config file."""
    return [n for n in names if not ((ts := TOOLSETS.get(n)) is not None and ts.posture)]
