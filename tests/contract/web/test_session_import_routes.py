"""P24-28: ``/api/session-import/*`` over the real app (sample files written here)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.web.routes import session_import
from tests.contract.web.rest_app import RestApp, build_rest_app, route_table


def _claude_file(home: Path) -> Path:
    path = home / ".claude" / "projects" / "-w" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    records: list[Any] = [
        {"type": "user", "cwd": "/w", "message": {"role": "user", "content": "Hello there"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "Hi."}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "user-home"


@pytest.fixture
def rest(scope: Scope, home: Path) -> Iterator[RestApp]:
    built = build_rest_app(scope, extras={"session_import.home": home})
    built.client.params = {"profile": "work"}  # the test root holds two profiles: writes name one
    yield built
    built.close()


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


def test_candidates_preview_and_import(rest: RestApp, home: Path) -> None:
    path = _claude_file(home)
    listed = rest.client.get("/api/session-import/candidates").json()
    assert listed["sources"] == ["claude-code", "codex"]
    assert [c["path"] for c in listed["candidates"]] == [str(path)]
    body = {"source": "claude-code", "path": str(path)}
    shown = rest.client.post("/api/session-import/preview", json=body).json()
    assert [m["role"] for m in shown["messages"]] == ["user", "assistant"]
    assert shown["title"] == "Hello there"
    done = rest.client.post("/api/session-import/import", json=body)
    assert done.status_code == 200 and done.json()["message_count"] == 2
    again = rest.client.post("/api/session-import/import", json=body)
    assert again.status_code == 409 and _code(again) == "already_imported"
    assert again.json()["detail"]["session_id"] == done.json()["session_id"]


def test_paths_outside_the_roots_and_unknown_formats_are_refused(
    rest: RestApp, home: Path, tmp_path: Path
) -> None:
    stray = tmp_path / "x.jsonl"
    stray.write_text("{}\n", encoding="utf-8")
    answer = rest.client.post(
        "/api/session-import/preview", json={"source": "claude-code", "path": str(stray)}
    )
    assert answer.status_code == 400 and _code(answer) == "not_a_candidate"
    odd = home / ".codex" / "sessions" / "rollout-1.jsonl"
    odd.parent.mkdir(parents=True)
    odd.write_text(json.dumps({"what": 1}) + "\n", encoding="utf-8")
    answer = rest.client.post(
        "/api/session-import/preview", json={"source": "codex", "path": str(odd)}
    )
    assert answer.status_code == 422 and _code(answer) == "unknown_format"
    bad = rest.client.get("/api/session-import/candidates", params={"source": "other"})
    assert bad.status_code == 400


def test_the_routes_need_the_token(rest: RestApp) -> None:
    assert rest.anonymous().get("/api/session-import/candidates").status_code == 401


def test_route_list_matches_the_app(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/session-import",)) == set(session_import.ROUTE_LIST)
