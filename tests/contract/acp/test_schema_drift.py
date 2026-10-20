"""P19-18: every ACP name the adapter uses is in the pinned vendor schema, in the tier we expect.

The snapshot under ``tests/fixtures/acp/`` is the vendor's own files at the tag the installed SDK
was generated from (stable and unstable schema halves and their method tables, Apache-2.0, see
THIRD_PARTY_NOTICES). Never edit them to make a test pass: a vendor change means a new SDK pin and
a new snapshot.

The names we use are collected from the adapter itself — the handler table, and the vendor-shaped
literals in ``src/indus/acp`` (``sessionUpdate`` kinds, tool kinds, permission option kinds, tool
call and plan statuses, stop reasons, the methods we call on the editor) — plus the capability
fields the real ``initialize`` answer sets and the client capability the adapter reads. A new
literal in the adapter is checked the day it is written.
"""

from __future__ import annotations

import ast
import copy
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
import acp.meta

from indus.acp import tool_kinds
from indus.acp.agent import (  # pyright: ignore[reportPrivateUsage]
    AcpAgent,
    _form_elicitation,
    default_handlers,
    initialize,
)
from indus.acp.backend import AcpBackend, HostSelector
from indus.acp.projection import _PLAN_STATUS  # pyright: ignore[reportPrivateUsage]
from indus.foundation.scope import Scope
from tests.unit.backends._aio import aio

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures" / "acp"
ACP_SRC = REPO / "src" / "indus" / "acp"
TAG = "v1.19.0"

UNSTABLE = frozenset(
    {
        "session/fork",
        "elicitation/create",
        "agentCapabilities.sessionCapabilities.fork",
        "clientCapabilities.elicitation",
        "clientCapabilities.elicitation.form",
    }
)
"""Names we use that the vendor ships only in the unstable half at this tag. Everything else we
use must be stable."""

CATEGORIES = (
    "method",
    "session update",
    "tool kind",
    "permission option kind",
    "tool call status",
    "plan entry status",
    "stop reason",
    "capability",
)

type Tiers = dict[str, str]
"""name -> "stable" | "unstable"."""


# -- the vendor side ------------------------------------------------------------------------------


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def snapshot() -> dict[str, Any]:
    return {
        "stable": _load(f"schema-{TAG}.json"),
        "unstable": _load(f"schema-{TAG}.unstable.json"),
        "meta": _load(f"meta-{TAG}.json"),
        "meta_unstable": _load(f"meta-{TAG}.unstable.json"),
    }


def _values(defn: Mapping[str, Any]) -> set[str]:
    """The string values an enum-like definition allows (``oneOf`` consts or ``enum``)."""
    found = {str(o["const"]) for o in defn.get("oneOf", ()) if "const" in o}
    return found | {str(v) for v in defn.get("enum", ())}


def _ref(schema: Mapping[str, Any], node: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The object definition ``node`` points at (through ``$ref`` / ``anyOf`` with null)."""
    if "$ref" in node:
        return schema["$defs"].get(str(node["$ref"]).rsplit("/", 1)[-1])
    for branch in (*node.get("anyOf", ()), *node.get("allOf", ())):
        target = _ref(schema, branch)
        if target is not None:
            return target
    return None


def _fields(schema: Mapping[str, Any], root: str, prefix: str, depth: int = 3) -> Iterator[str]:
    """Dotted capability paths under the definition ``root``."""
    defn = schema["$defs"].get(root)
    if defn is None:
        return
    stack: list[tuple[Mapping[str, Any], str, int]] = [(defn, prefix, depth)]
    while stack:
        node, path, left = stack.pop()
        for name, prop in node.get("properties", {}).items():
            if name == "_meta":
                continue
            here = f"{path}.{name}"
            yield here
            target = _ref(schema, prop)
            if target is not None and left > 1:
                stack.append((target, here, left - 1))


def _half(schema: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, set[str]]:
    defs = schema["$defs"]
    methods: set[str] = set()
    for table in ("agentMethods", "clientMethods", "protocolMethods"):
        methods |= set(meta.get(table, {}).values())
    updates = {
        str(o["properties"]["sessionUpdate"]["const"])
        for o in defs["SessionUpdate"]["oneOf"]
        if "sessionUpdate" in o.get("properties", {})
    }
    capabilities = set(_fields(schema, "AgentCapabilities", "agentCapabilities"))
    capabilities |= set(_fields(schema, "ClientCapabilities", "clientCapabilities"))
    return {
        "method": methods,
        "session update": updates,
        "tool kind": _values(defs["ToolKind"]),
        "permission option kind": _values(defs["PermissionOptionKind"]),
        "tool call status": _values(defs["ToolCallStatus"]),
        "plan entry status": _values(defs["PlanEntryStatus"]),
        "stop reason": _values(defs["StopReason"]),
        "capability": capabilities,
    }


def vendor_names(snap: Mapping[str, Any]) -> dict[str, Tiers]:
    """Category -> name -> tier, from a snapshot (stable wins when a name is in both halves)."""
    stable = _half(snap["stable"], snap["meta"])
    unstable = _half(snap["unstable"], snap["meta_unstable"])
    out: dict[str, Tiers] = {}
    for category in CATEGORIES:
        tiers: Tiers = {name: "unstable" for name in unstable[category]}
        tiers.update({name: "stable" for name in stable[category]})
        out[category] = tiers
    return out


# -- our side ------------------------------------------------------------------------------------


@dataclass
class Used:
    names: dict[str, set[str]] = field(
        default_factory=lambda: {category: set[str]() for category in CATEGORIES}
    )

    def add(self, category: str, *values: str) -> None:
        self.names[category].update(values)


def _literals(node: ast.AST | None) -> list[str]:
    """String constants of ``node`` (a literal, or both arms of ``a if c else b``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _literals(node.body) + _literals(node.orelse)
    return []


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


_UPDATE_NAME = re.compile(r"^[a-z]+(?:_[a-z]+)*_(?:chunk|update)$")


def scan_adapter(root: Path = ACP_SRC) -> Used:
    """Vendor-shaped literals written in the adapter's modules (see the module notes)."""
    used = Used()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _UPDATE_NAME.match(node.value):
                    used.add("session update", node.value)
            elif isinstance(node, ast.Dict):
                entries = {
                    k.value: v
                    for k, v in zip(node.keys, node.values, strict=True)
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                if "sessionUpdate" in entries:
                    used.add("session update", *_literals(entries["sessionUpdate"]))
                if "stopReason" in entries:
                    used.add("stop reason", *_literals(entries["stopReason"]))
                if "optionId" in entries and "kind" in entries:
                    used.add("permission option kind", *_literals(entries["kind"]))
                if "toolCallId" in entries or "sessionUpdate" in entries:
                    if "kind" in entries:
                        used.add("tool kind", *_literals(entries["kind"]))
                    if "status" in entries:
                        used.add("tool call status", *_literals(entries["status"]))
            elif isinstance(node, ast.Call) and node.args:
                callee = _callee(node)
                first = _literals(node.args[0])
                if callee in ("request", "notify"):
                    used.add("method", *(m for m in first if "/" in m))
                elif callee == "_answer":  # the prompt reply builder
                    used.add("stop reason", *first)
            elif isinstance(node, ast.AnnAssign | ast.Assign):
                targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.endswith("_METHOD"):
                        used.add("method", *_literals(node.value))
    return used


def _flatten(prefix: str, value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, inner in value.items():  # pyright: ignore[reportUnknownVariableType]
            if key == "_meta":
                continue
            here = f"{prefix}.{key}"
            yield here
            yield from _flatten(here, inner)


async def _no_backend() -> AcpBackend:
    raise AssertionError("initialize never builds a backend")


async def used_names(scope: Scope) -> dict[str, set[str]]:
    """Everything the adapter uses (see the module notes)."""
    used = scan_adapter()
    used.add("method", *default_handlers())
    used.add("method", "session/update")  # ``AcpAgent.update``
    used.add("tool kind", *tool_kinds.KINDS.values(), tool_kinds.kind_of("no such tool"))
    used.add("plan entry status", *_PLAN_STATUS.values())
    selector = HostSelector(
        local_only=True, allow_fallback=False, attached=_no_backend, local=_no_backend
    )
    agent = AcpAgent(scope, selector, env={})
    answer = await initialize(agent, {"protocolVersion": acp.PROTOCOL_VERSION})
    used.add("capability", *_flatten("agentCapabilities", answer["agentCapabilities"]))
    read = {"clientCapabilities": {"elicitation": {"form": {}}}}
    assert _form_elicitation(read)  # the client capability the adapter reads
    used.add("capability", "clientCapabilities.elicitation", "clientCapabilities.elicitation.form")
    return used.names


def drift(vendor: Mapping[str, Tiers], used: Mapping[str, set[str]]) -> list[str]:
    """One line per used name that is missing from the schema or sits in another tier."""
    problems: list[str] = []
    for category in CATEGORIES:
        known = vendor[category]
        for name in sorted(used.get(category, ())):
            want = "unstable" if name in UNSTABLE else "stable"
            have = known.get(name)
            if have is None:
                problems.append(f"{category} {name!r} is not in the ACP schema {TAG}")
            elif have != want:
                problems.append(
                    f"{category} {name!r} is {have} in the ACP schema, we expect {want}"
                )
    return problems


# -- tests ---------------------------------------------------------------------------------------


def test_the_sdk_was_generated_from_the_snapshot_tag() -> None:
    snap = snapshot()
    header = Path(acp.meta.__file__).read_text(encoding="utf-8").splitlines()[:3]
    assert f"# Schema ref: refs/tags/schema-{TAG}" in header
    assert acp.PROTOCOL_VERSION == snap["meta"]["version"] == snap["meta_unstable"]["version"]
    vendor_agent = set(snap["meta_unstable"]["agentMethods"].values())
    vendor_client = set(snap["meta_unstable"]["clientMethods"].values())
    assert set(acp.meta.AGENT_METHODS.values()) == vendor_agent
    assert set(acp.meta.CLIENT_METHODS.values()) == vendor_client


@aio
async def test_every_name_the_adapter_uses_is_in_the_schema_with_its_tier(scope: Scope) -> None:
    used = await used_names(scope)
    assert drift(vendor_names(snapshot()), used) == []


@aio
async def test_the_collection_sees_what_the_adapter_sends(scope: Scope) -> None:
    used = await used_names(scope)
    assert {
        "initialize",
        "authenticate",
        "session/new",
        "session/load",
        "session/resume",
        "session/fork",
        "session/list",
        "session/prompt",
        "session/cancel",
        "session/set_mode",
        "session/set_config_option",
        "session/update",
        "session/request_permission",
        "elicitation/create",
    } <= used["method"]
    assert {
        "user_message_chunk",
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "available_commands_update",
        "session_info_update",
        "usage_update",
    } <= used["session update"]
    assert {"read", "edit", "search", "execute", "fetch", "think", "other"} <= used["tool kind"]
    assert used["permission option kind"] == {
        "allow_once",
        "allow_always",
        "reject_once",
        "reject_always",
    }
    assert {"pending", "in_progress", "completed", "failed"} <= used["tool call status"]
    assert {"end_turn", "cancelled"} <= used["stop reason"]
    assert {
        "agentCapabilities.loadSession",
        "agentCapabilities.promptCapabilities.image",
        "agentCapabilities.sessionCapabilities.fork",
        "agentCapabilities.sessionCapabilities.list",
        "agentCapabilities.sessionCapabilities.resume",
    } <= used["capability"]


@aio
async def test_removing_a_kind_from_the_snapshot_fails_the_check(scope: Scope) -> None:
    used = await used_names(scope)
    snap = copy.deepcopy(snapshot())
    for half in ("stable", "unstable"):
        kinds = snap[half]["$defs"]["ToolKind"]
        kinds["oneOf"] = [o for o in kinds["oneOf"] if o.get("const") != "execute"]
    assert drift(vendor_names(snap), used) == [
        f"tool kind 'execute' is not in the ACP schema {TAG}"
    ]


@aio
async def test_a_name_moved_to_another_tier_fails_the_check(scope: Scope) -> None:
    used = await used_names(scope)
    snap = copy.deepcopy(snapshot())
    snap["meta"]["agentMethods"]["session_fork"] = "session/fork"  # promoted to stable
    del snap["meta"]["agentMethods"]["session_list"]  # demoted to unstable only
    assert drift(vendor_names(snap), used) == [
        "method 'session/fork' is stable in the ACP schema, we expect unstable",
        "method 'session/list' is unstable in the ACP schema, we expect stable",
    ]
