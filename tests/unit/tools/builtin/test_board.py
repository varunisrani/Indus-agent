"""P24-13: the `kanban_*` tools — worker/orchestrator split, ownership, handoffs (R02.25)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.safety.url_policy import Allowed
from indus.state.board import ATTACHMENT_MAX_BYTES, BOARD_ENV, BoardStore
from indus.tools import toolsets
from indus.tools.builtin import BUILTIN_MODULES, load_builtins
from indus.tools.builtin import board as board_tools
from indus.tools.registry import ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    board_tools.register_tools(reg)
    return reg


@pytest.fixture
def store(scope: Scope) -> Iterator[BoardStore]:
    opened = BoardStore(scope)
    yield opened
    opened.close()


def _call(registry: ToolRegistry, name: str, scope: Scope, **args: Any) -> dict[str, Any]:
    entry = registry.get(name)
    assert entry is not None
    result = entry.handler(args, scope=scope)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return json.loads(result)


def _worker_scope(scope: Scope, store: BoardStore, task_id: str) -> Scope:
    task = store.require(task_id)
    pins = {
        BOARD_ENV["task"]: task_id,
        BOARD_ENV["run"]: str(task.current_run_id),
        BOARD_ENV["claim"]: task.claim_lock or "",
        BOARD_ENV["slug"]: store.slug,
        BOARD_ENV["root"]: str(store.root),
    }
    return scope.with_env(scope.env.with_overlay(pins))


def test_all_fifteen_tools_register_from_the_builtin_list() -> None:
    assert any(m.path == "indus.tools.builtin.board" for m in BUILTIN_MODULES)
    reg = ToolRegistry()
    load_builtins(reg)
    names = set(reg.names())
    assert set(board_tools.TOOL_NAMES) <= names
    assert len(board_tools.TOOL_NAMES) == 15
    assert reg.names_in_toolset("kanban") == ["kanban_list", "kanban_unblock"]
    assert len(reg.names_in_toolset("kanban-worker")) == 13


def test_worker_session_lacks_list_and_unblock(registry: ToolRegistry, scope: Scope) -> None:
    worker = toolsets.select(["core", "kanban-worker"], registry=registry, scope=scope).tools
    assert "kanban_complete" in worker and "kanban_heartbeat" in worker
    assert not {"kanban_list", "kanban_unblock"} & worker
    orchestrator = toolsets.select(["kanban"], registry=registry, scope=scope).tools
    assert set(board_tools.TOOL_NAMES) == orchestrator


def test_schedule_present_when_the_toolset_is_on(registry: ToolRegistry, scope: Scope) -> None:
    # Membership comes from the registry (R02.25): the static sets name no tools at all.
    assert toolsets.TOOLSETS["kanban-worker"].tools == ()
    on = toolsets.select(["kanban-worker"], registry=registry, scope=scope).tools
    assert "kanban_schedule" in on
    # Opt-in: "all" never brings board tools into an ordinary session.
    everything = toolsets.select(None, registry=registry, scope=scope).tools
    assert not set(board_tools.TOOL_NAMES) & everything
    assert not set(board_tools.TOOL_NAMES) & set(toolsets.resolve("indus-cli", registry, scope))


def test_complete_without_a_handoff_is_refused(
    registry: ToolRegistry, scope: Scope, store: BoardStore
) -> None:
    task = store.create("write it", assignee="work")
    assert store.claim(task.id, "lock-1") is not None
    worker = _worker_scope(scope, store, task.id)
    refused = _call(registry, "kanban_complete", worker)
    assert "handoff" in refused["error"]
    assert store.require(task.id).status == "running"
    done = _call(registry, "kanban_complete", worker, summary="wrote it", metadata={"n": 1})
    assert done == {"ok": True, "task_id": task.id, "status": "done", "attachments": []}


def test_worker_cannot_touch_another_task(
    registry: ToolRegistry, scope: Scope, store: BoardStore
) -> None:
    mine = store.create("mine", assignee="work")
    theirs = store.create("theirs", assignee="work")
    store.claim(mine.id, "lock-1")
    worker = _worker_scope(scope, store, mine.id)
    for tool, args in (
        ("kanban_complete", {"summary": "x"}),
        ("kanban_block", {"reason": "x"}),
        ("kanban_heartbeat", {}),
        ("kanban_attach", {"filename": "a.txt", "content_base64": "eA=="}),
    ):
        answer = _call(registry, tool, worker, task_id=theirs.id, **args)
        assert "may not change" in answer["error"], tool
    # Comments and new tasks are the hand-off channel.
    assert _call(registry, "kanban_comment", worker, task_id=theirs.id, body="fyi")["ok"]
    made = _call(registry, "kanban_create", worker, title="follow-up", assignee="work")
    assert made["ok"] and made["status"] == "ready"
    # The orchestrator pair refuses inside a worker even if it is offered.
    assert "orchestrators" in _call(registry, "kanban_list", worker)["error"]
    assert "orchestrators" in _call(registry, "kanban_unblock", worker, task_id=theirs.id)["error"]
    # Another board is out of reach for a worker.
    assert "board" in _call(registry, "kanban_show", worker, board="other")["error"]


def test_lifecycle_tools_need_the_pinned_run(
    registry: ToolRegistry, scope: Scope, store: BoardStore
) -> None:
    task = store.create("t", assignee="work")
    store.claim(task.id, "lock-1")
    stale = _worker_scope(scope, store, task.id)
    # The task was reclaimed and claimed again by a new run: the old worker's run id is stale.
    run_id = store.require(task.id).current_run_id
    assert run_id is not None
    store.release_run(task.id, run_id, outcome="reclaimed")
    store.claim(task.id, "lock-2")
    answer = _call(registry, "kanban_complete", stale, summary="late")
    assert "no longer owns" in answer["error"]
    unbound = stale.with_env(stale.env.with_overlay({BOARD_ENV["run"]: None}))
    assert "cannot name the run" in _call(registry, "kanban_block", unbound, reason="x")["error"]
    current = _worker_scope(scope, store, task.id)
    blocked = _call(registry, "kanban_block", current, reason="need a key", kind="capability")
    assert blocked["status"] == "blocked" and blocked["block_kind"] == "capability"


def test_comment_author_is_the_session_identity(
    registry: ToolRegistry, scope: Scope, store: BoardStore
) -> None:
    task = store.create("t")
    _call(registry, "kanban_comment", scope, task_id=task.id, body="hi", author="system")
    assert store.comments(task.id)[0].author == scope.profile_id


def test_attach_cap_and_url_policy(registry: ToolRegistry, scope: Scope, store: BoardStore) -> None:
    task = store.create("t")
    small = base64.b64encode(b"hello").decode()
    saved = _call(registry, "kanban_attach", scope, task_id=task.id, filename="a.txt",
                  content_base64=small)  # fmt: skip
    assert saved["attachment"]["size"] == 5
    big = base64.b64encode(b"\0" * (ATTACHMENT_MAX_BYTES + 1)).decode()
    refused = _call(registry, "kanban_attach", scope, task_id=task.id, filename="b.bin",
                    content_base64=big)  # fmt: skip
    assert "limit" in refused["error"]
    bad = _call(
        registry, "kanban_attach", scope, task_id=task.id, filename="c", content_base64="!!"
    )
    assert "base64" in bad["error"]
    private = _call(registry, "kanban_attach_url", scope, task_id=task.id, url="http://10.0.0.5/x")
    assert "refused" in private["error"]
    ftp = _call(registry, "kanban_attach_url", scope, task_id=task.id, url="ftp://example.com/x")
    assert "http" in ftp["error"]
    assert len(store.attachments(task.id)) == 1


def test_attach_url_stores_what_the_fetch_returned(
    scope: Scope, store: BoardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = store.create("t")

    async def fetch(url: str, where: Scope, cap: int) -> tuple[bytes, str | None]:
        assert cap == ATTACHMENT_MAX_BYTES
        return b"%PDF-1.7", "application/pdf"

    async def public(url: str, where: Scope) -> Allowed:
        return Allowed(url, "files.example", ("93.184.215.14",))

    monkeypatch.setattr(board_tools, "check_url_async", public)
    out = asyncio.run(
        board_tools.kanban_attach_url(
            {"task_id": task.id, "url": "https://files.example/report.pdf"},
            scope=scope,
            fetch=fetch,
        )
    )
    saved = json.loads(out)["attachment"]
    assert saved["filename"] == "report.pdf" and saved["content_type"] == "application/pdf"


def test_review_flow_through_the_tools(
    registry: ToolRegistry, scope: Scope, store: BoardStore
) -> None:
    reviewer_home = scope.root / "profiles" / "qa"
    reviewer_home.mkdir(parents=True)
    (reviewer_home / "profile.yaml").touch()
    task = store.create("feature", assignee="work")
    store.claim(task.id, "lock-dev")
    dev = _worker_scope(scope, store, task.id)
    missing = _call(registry, "kanban_request_review", dev, summary="done", reviewer="nobody")
    assert "no profile" in missing["error"]
    asked = _call(registry, "kanban_request_review", dev, summary="done, tested", reviewer="qa")
    assert asked["status"] == "review" and asked["reviewer"] == "qa"
    store.claim_review(task.id, "lock-qa")
    qa = _worker_scope(scope, store, task.id)
    back = _call(registry, "kanban_request_changes", qa, reason="add docs")
    assert back["status"] == "ready" and back["implementer"] == "work"


def test_show_list_link_unblock_for_an_orchestrator(
    registry: ToolRegistry, scope: Scope, store: BoardStore
) -> None:
    hint = _call(registry, "kanban_show", scope)
    assert hint["task"] is None and "kanban_list" in hint["hint"]
    a = store.create("a", assignee="work")
    b = store.create("b", assignee="work")
    linked = _call(registry, "kanban_link", scope, parent_id=a.id, child_id=b.id)
    assert linked["gated"] is True
    cycle = _call(registry, "kanban_link", scope, parent_id=b.id, child_id=a.id)
    assert "cycle" in cycle["error"]
    shown = _call(registry, "kanban_show", scope, task_id=b.id)
    assert shown["task"]["status"] == "todo" and shown["parents"] == [a.id]
    listed = _call(registry, "kanban_list", scope, limit=1)
    assert listed["count"] == 1 and listed["truncated"] is True
    assert "limit" in _call(registry, "kanban_list", scope, limit=999)["error"]
    _call(registry, "kanban_block", scope, task_id=a.id, reason="hold", kind="needs_input")
    unblocked = _call(registry, "kanban_unblock", scope, task_id=a.id)
    assert unblocked["status"] == "ready"
    sched = _call(registry, "kanban_schedule", scope, task_id=a.id, reason="later")
    assert sched["status"] == "blocked" and sched["scheduled"] is True


def test_create_subscribes_the_calling_chat(scope: Scope, store: BoardStore) -> None:
    out = board_tools.kanban_create(
        {"title": "ping me", "assignee": "work"},
        scope=scope,
        delivery_origin={"platform": "telegram", "chat_id": "42", "thread_id": "7"},
    )
    made = json.loads(out)
    assert made["subscribed"] is True
    sub = store.subscriptions(made["task_id"])[0]
    assert (sub.platform, sub.chat_id, sub.thread_id) == ("telegram", "42", "7")
