"""``/api/learning``: the learning map's read (P24-28) over the learning timeline (P09-23).

- ``GET /api/learning/graph``: ``{nodes, edges}`` — every node ``indus journey`` shows (skills
  with a learning signal and every memory entry, ``indus.learning.journey.nodes``) in the shape the
  desktop map draws: ``{id, kind, label, category, useCount, createdBy, timestamp, enabled}``.
  Skill ids are ``skill:<name>`` (the same ids the map gives skills when it falls back to the
  skills list); memory ids are the journey's ``memory:<store>:<n>:<sha8>``. ``createdBy`` is
  ``agent`` for a skill the background review wrote and for every memory entry (the agent writes
  both stores), ``user`` for a skill made with ``/learn`` or by hand, null when unknown.
  ``enabled`` is false for a skill listed in ``skills.disabled``.
- ``DELETE /api/learning/nodes/{node_id}``: removes a memory entry or archives a skill, exactly as
  ``indus journey delete`` does (``journey.delete_node``; a pinned skill is refused, an archived
  one comes back with ``indus curator restore``). A stale or unknown id is 404; any other refusal
  409, with the learning layer's own message.

Edges: the learning layer records no "learned with" link between a memory and a skill, so the
graph links what it knows belongs together — skills of one category, and the entries of one
memory store — each group as a star around its first member. Labels, ids and texts never leave
the profile except through this authenticated read; the map's share code carries none of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException

from indus.foundation import config as config_module
from indus.foundation.scope import Scope
from indus.learning import journey
from indus.web.scope import read_scope, write_scope

__all__ = ["ROUTE_LIST", "SKILL_PREFIX", "graph_of", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/learning/graph", True),
    ("DELETE", "/api/learning/nodes/{node_id}", True),
)
"""``(method, path, scoped)`` of every route here."""

SKILL_PREFIX: Final = "skill:"

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def _disabled(scope: Scope) -> set[str]:
    raw: object = config_module.load(scope).get("skills.disabled", [])
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw}  # pyright: ignore[reportUnknownVariableType]


def _created_by(node: journey.Node) -> str | None:
    if node.kind == "memory" or node.origin == "agent":
        return "agent"
    if node.origin == "learn":
        return "user"
    return None


def _stars(groups: dict[str, list[str]]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for members in groups.values():
        edges.extend({"from": members[0], "to": other} for other in members[1:])
    return edges


def graph_of(found: Sequence[journey.Node], disabled: set[str]) -> dict[str, Any]:
    """The map's ``{nodes, edges}`` for journey nodes (oldest first, so stars grow in time)."""
    ordered = sorted(found, key=lambda n: (n.timestamp is None, n.timestamp or 0.0, n.id))
    nodes: list[dict[str, Any]] = []
    groups: dict[str, list[str]] = {}
    for node in ordered:
        skill = node.kind == "skill"
        node_id = f"{SKILL_PREFIX}{node.id}" if skill else node.id
        nodes.append(
            {
                "id": node_id,
                "kind": "skill" if skill else "memory",
                "label": node.label,
                "category": node.category,
                "useCount": node.use_count,
                "createdBy": _created_by(node),
                "timestamp": node.timestamp,
                "enabled": not (skill and node.id in disabled),
            }
        )
        key = f"skill:{node.category}" if skill else f"memory:{node.store or ''}"
        groups.setdefault(key, []).append(node_id)
    return {"nodes": nodes, "edges": _stars(groups)}


@router.get("/api/learning/graph")
def learning_graph(scope: ReadScope) -> dict[str, Any]:
    return graph_of(journey.nodes(scope), _disabled(scope))


@router.delete("/api/learning/nodes/{node_id}")
def delete_learning_node(node_id: str, scope: WriteScope) -> dict[str, Any]:
    target = node_id.removeprefix(SKILL_PREFIX)
    if target == "" or (target == node_id and not node_id.startswith("memory:")):
        _fail(404, "node_not_found", "No learned item has this id.")
    try:
        journey.node_text(scope, target)  # unknown, stale or not learned here: 404
    except journey.JourneyError as exc:
        _fail(404, "node_not_found", str(exc))
    try:
        said = journey.delete_node(scope, target)
    except journey.JourneyError as exc:
        _fail(409, "node_refused", str(exc))
    return {"ok": True, "message": said}
