"""P24-28 wiring (D-16.799): ``/api/learning/*`` — the learning map's read and delete over the real
app, on the same nodes ``indus journey`` shows."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation import config
from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.learning.journey import nodes
from indus.learning.memory.store import MemoryStore, Op, Target
from indus.web.routes import learning
from tests.contract.web.rest_app import RestApp, build_rest_app, route_table
from tests.unit.learning.curator._helpers_f import (
    add_skill,
    clean_registries,  # noqa: F401  (autouse fixture)
)

READ = {"profile": "work"}


def remember(scope: Scope, target: Target, *texts: str) -> None:
    result = MemoryStore.for_scope(scope).apply(target, [Op("add", content=t) for t in texts])
    assert result.ok, result.message


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    config_loader.reset()
    add_skill(scope, "review-made", origin="agent", created_days=3, use_count=2)
    add_skill(scope, "taught", origin="learn", created_days=2)
    add_skill(scope, "pinned-one", origin="agent", created_days=1, pinned=True)
    remember(scope, "memory", "The repo uses uv.", "Tests run with pytest -q.")
    remember(scope, "user", "Prefers short answers.")
    config.save(scope, {"skills": {"disabled": ["taught"]}})
    built = build_rest_app(scope)
    yield built
    built.close()
    config_loader.reset()


def _graph(rest: RestApp) -> dict[str, Any]:
    answer = rest.client.get("/api/learning/graph", params=READ)
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_route_list_is_pinned(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/learning",)) == set(learning.ROUTE_LIST)


def test_the_graph_has_the_journey_nodes_in_the_map_shape(rest: RestApp, scope: Scope) -> None:
    graph = _graph(rest)
    by_id = {n["id"]: n for n in graph["nodes"]}
    journey_ids = {n.id if n.kind == "memory" else f"skill:{n.id}" for n in nodes(scope)}
    assert set(by_id) == journey_ids
    made = by_id["skill:review-made"]
    assert made["kind"] == "skill" and made["createdBy"] == "agent" and made["useCount"] == 2
    assert made["enabled"] is True and isinstance(made["timestamp"], float)
    taught = by_id["skill:taught"]
    assert taught["createdBy"] == "user" and taught["enabled"] is False
    memories = [n for n in graph["nodes"] if n["kind"] == "memory"]
    assert {m["label"] for m in memories} == {
        "The repo uses uv.",
        "Tests run with pytest -q.",
        "Prefers short answers.",
    }
    assert all(m["createdBy"] == "agent" for m in memories)
    # the two notes of the agent's own store are linked; the user store's entry stands alone
    notes = sorted(m["id"] for m in memories if m["id"].startswith("memory:memory:"))
    linked = {frozenset((e["from"], e["to"])) for e in graph["edges"]}
    assert frozenset(notes) in linked
    assert all(e["from"] in by_id and e["to"] in by_id for e in graph["edges"])


def test_delete_removes_a_memory_and_archives_a_skill(rest: RestApp, scope: Scope) -> None:
    graph = _graph(rest)
    entry = next(n for n in graph["nodes"] if n["label"] == "Prefers short answers.")
    gone = rest.client.delete(f"/api/learning/nodes/{entry['id']}", params=READ)
    assert gone.status_code == 200, gone.text
    assert MemoryStore.for_scope(scope).entries("user") == []
    stale = rest.client.delete(f"/api/learning/nodes/{entry['id']}", params=READ)
    assert stale.status_code == 404 and stale.json()["detail"]["code"] == "node_not_found"
    archived = rest.client.delete("/api/learning/nodes/skill:review-made", params=READ)
    assert archived.status_code == 200 and "restore" in archived.json()["message"]
    assert "skill:review-made" not in {n["id"] for n in _graph(rest)["nodes"]}
    pinned = rest.client.delete("/api/learning/nodes/skill:pinned-one", params=READ)
    assert pinned.status_code == 409 and "pinned" in pinned.json()["detail"]["message"]
    unknown = rest.client.delete("/api/learning/nodes/review-made", params=READ)
    assert unknown.status_code == 404
