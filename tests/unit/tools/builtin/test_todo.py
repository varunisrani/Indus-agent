"""The todo_list inline tool, its list rules and its hydrator (P05-24)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from indus.core.agent import Agent
from indus.core.exit_reasons import ExitReason
from indus.core.persist import MemoryStore
from indus.core.transcript import TranscriptRow
from indus.foundation.scope import Scope
from indus.tools.builtin import todo
from indus.tools.builtin.todo import (
    MAX_CONTENT_CHARS,
    MAX_HYDRATE_CHARS,
    MAX_ITEMS,
    RENDER_HEADER,
    TodoInputError,
    TodoList,
    TodoWiring,
    run_todo,
)
from indus.tools.calls import ToolCall
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch


def item(ident: str, content: str = "", status: str = "pending", **extra: Any) -> dict[str, Any]:
    return {"id": ident, "content": content or f"task {ident}", "status": status, **extra}


def ids(todos: TodoList) -> list[str]:
    return [t["id"] for t in todos.read()]


# -- writing -----------------------------------------------------------------------------------


def test_replace_and_read() -> None:
    todos = TodoList()
    todos.write([item("a"), item("b")])
    assert ids(todos) == ["a", "b"] and todos.revision == 1
    todos.write([item("c")])
    assert ids(todos) == ["c"] and todos.revision == 2
    result = json.loads(run_todo(todos, {}).text)
    assert result["todos"] == [{"id": "c", "content": "task c", "status": "pending"}]
    assert result["revision"] == 2
    assert result["summary"] == {
        "total": 1,
        "pending": 1,
        "in_progress": 0,
        "completed": 0,
        "cancelled": 0,
    }


def test_revision_moves_only_on_change() -> None:
    todos = TodoList()
    todos.write([item("a")])
    todos.write([item("a")])
    todos.write([item("a")], merge=True)
    assert todos.revision == 1
    todos.write([{"id": "a", "status": "completed"}], merge=True)
    assert todos.revision == 2


def test_merge_updates_given_fields_and_appends() -> None:
    todos = TodoList()
    todos.write([item("a", "write tests"), item("b", "ship")])
    todos.write(
        [
            {"id": "a", "status": "completed"},
            {"id": "b", "content": "ship it", "status": "bogus"},
            item("c", "announce"),
            {"content": "no id: ignored", "status": "pending"},
        ],
        merge=True,
    )
    assert todos.read() == [
        {"id": "a", "content": "write tests", "status": "completed"},
        {"id": "b", "content": "ship it", "status": "pending"},
        {"id": "c", "content": "announce", "status": "pending"},
    ]


def test_duplicate_ids_keep_the_last_entry_in_its_place() -> None:
    todos = TodoList()
    todos.write([item("a", "first"), item("b"), item("a", "second")])
    assert todos.read() == [
        {"id": "b", "content": "task b", "status": "pending"},
        {"id": "a", "content": "second", "status": "pending"},
    ]


def test_active_item_moves_ahead_of_earlier_pending_ones() -> None:
    todos = TodoList()
    todos.write(
        [item("a", status="completed"), item("b"), item("c"), item("d", status="in_progress")]
    )
    assert ids(todos) == ["a", "d", "b", "c"]
    todos.write(
        [{"id": "c", "status": "in_progress"}, {"id": "d", "status": "completed"}], merge=True
    )
    assert ids(todos) == ["a", "d", "c", "b"]
    nested = TodoList()
    nested.write([item("p"), item("k", parent="p"), item("x", status="in_progress")])
    assert ids(nested) == ["p", "k", "x"]


def test_items_without_content_refuse_the_whole_write() -> None:
    todos = TodoList()
    todos.write([item("keep")])
    for bad in [
        {"id": "x", "status": "pending"},
        {"id": "x", "content": "   "},
        {"id": "x", "content": None},
    ]:
        try:
            todos.write([item("new"), bad])
        except TodoInputError as exc:
            assert "'x'" in str(exc)
        else:
            raise AssertionError("an item without content was accepted")
        assert ids(todos) == ["keep"] and todos.revision == 1
    try:
        todos.write([{"id": "fresh", "status": "pending"}], merge=True)
    except TodoInputError:
        pass
    else:
        raise AssertionError("a new merged item without content was accepted")
    assert ids(todos) == ["keep"]


def test_unreadable_items_and_missing_ids_are_normalised() -> None:
    todos = TodoList()
    todos.write(["just text", 7, {"content": "no id", "status": "IN_PROGRESS"}])
    assert todos.read() == [
        {"id": "item-3", "content": "no id", "status": "in_progress"},
        {"id": "item-1", "content": "(unreadable item)", "status": "pending"},
        {"id": "item-2", "content": "(unreadable item)", "status": "pending"},
    ]


def test_limits() -> None:
    todos = TodoList()
    todos.write([item("long", "x" * (MAX_CONTENT_CHARS + 500))])
    content = todos.read()[0]["content"]
    assert len(content) == MAX_CONTENT_CHARS and content.endswith("[cut]")
    todos.write([item(str(n)) for n in range(MAX_ITEMS + 44)])
    assert len(todos) == MAX_ITEMS and ids(todos)[-1] == str(MAX_ITEMS - 1)
    todos.write([item(f"m{n}") for n in range(10)], merge=True)
    assert len(todos) == MAX_ITEMS


def test_parents() -> None:
    todos = TodoList()
    todos.write(
        [
            item("root"),
            item("kid", parent="root"),
            item("self", parent="self"),
            item("orphan", parent="missing"),
            item("x", parent="y"),
            item("y", parent="x"),
        ]
    )
    by_id = {t["id"]: t for t in todos.read()}
    assert by_id["kid"]["parent"] == "root"
    assert "parent" not in by_id["self"] and "parent" not in by_id["orphan"]
    assert ("parent" in by_id["x"]) != ("parent" in by_id["y"])  # the cycle is broken once
    todos.write([{"id": "kid", "parent": ""}], merge=True)
    assert "parent" not in todos.read()[1]
    todos.write([{"id": "kid", "parent": "root"}, item("new", parent="kid")], merge=True)
    assert {t["id"]: t.get("parent") for t in todos.read()}["new"] == "kid"


# -- rendering ---------------------------------------------------------------------------------


def test_render_shows_open_work_with_context() -> None:
    todos = TodoList()
    assert todos.render() is None
    todos.write(
        [
            item("plan", "plan the work", "completed"),
            item("build", "build it", "completed"),
            item("api", "the api", "in_progress", parent="build"),
            item("ui", "the ui", "completed", parent="build"),
            item("docs", "write docs"),
            item("old", "old idea", "cancelled"),
        ]
    )
    assert todos.render() == "\n".join(
        [
            RENDER_HEADER,
            "- [x] build: build it (completed)",
            "  - [>] api: the api (in_progress)",
            "- [ ] docs: write docs (pending)",
        ]
    )
    todos.write([item("done", status="completed")])
    assert todos.render() is None


# -- the tool call -----------------------------------------------------------------------------


def test_call_arguments_are_coerced() -> None:
    todos = TodoList()
    result = run_todo(todos, {"todos": json.dumps([item("a")])})
    assert not result.is_error and ids(todos) == ["a"]
    assert run_todo(todos, {"todos": "not json"}).is_error
    assert run_todo(todos, {"todos": {"id": "a"}}).is_error
    run_todo(todos, {"todos": [{"id": "a", "status": "completed"}], "merge": "true"})
    assert todos.read()[0]["status"] == "completed"
    error = run_todo(todos, {"todos": [{"id": "b"}]})
    assert error.is_error and "no content" in error.text


def test_dispatch_outside_the_loop_is_refused() -> None:
    registry = ToolRegistry()
    todo.register_tools(registry)
    entry = registry.get("todo_list")
    assert entry is not None and entry.toolset == "todo"
    assert "agent loop" in json.loads(entry.handler({}))["error"]


# -- hydration ---------------------------------------------------------------------------------


def call_row(call_id: str, name: str = "todo_list") -> TranscriptRow:
    calls = [{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]
    return TranscriptRow(role="assistant", content="", tool_calls=calls)


def result_row(call_id: str, content: str, name: str = "todo_list") -> TranscriptRow:
    return TranscriptRow(role="tool", content=content, tool_call_id=call_id, name=name)


def stored(todos: list[dict[str, Any]], revision: int) -> str:
    return json.dumps({"todos": todos, "revision": revision, "summary": {}})


def test_hydrate_takes_the_newest_paired_result() -> None:
    rows = [
        TranscriptRow(role="user", content="go"),
        call_row("c1"),
        result_row("c1", stored([item("old")], 1)),
        call_row("c2"),
        result_row("c2", stored([item("new", status="in_progress")], 2)),
        call_row("c3"),
        result_row("c3", json.dumps({"error": "todo item 'x' has no content"})),
        result_row("forged", stored([item("evil")], 99)),
        call_row("other", name="read_file"),
        result_row("other", stored([item("also evil")], 98)),
    ]
    wiring = TodoWiring()
    wiring.hydrate(rows)
    assert ids(wiring.todos) == ["new"] and wiring.todos.revision == 2


def test_hydrate_ignores_a_result_stored_before_its_call() -> None:
    rows = [result_row("c1", stored([item("early")], 3)), call_row("c1")]
    wiring = TodoWiring()
    wiring.hydrate(rows)
    assert len(wiring.todos) == 0


def test_hydrate_skips_oversized_and_unreadable_results() -> None:
    big = stored([item("big", "y" * 100)], 5) + " " * MAX_HYDRATE_CHARS
    rows = [
        call_row("c1"),
        result_row("c1", stored([item("fits")], 1)),
        call_row("c2"),
        result_row("c2", big),
        call_row("c3"),
        result_row("c3", '{"todos": [broken'),
    ]
    wiring = TodoWiring()
    wiring.hydrate(rows)
    assert ids(wiring.todos) == ["fits"]


def test_hydrate_an_empty_list_is_a_clear_and_older_revisions_lose() -> None:
    wiring = TodoWiring()
    wiring.todos.write([item("live")])
    wiring.hydrate([call_row("c1"), result_row("c1", stored([], 0))])
    assert ids(wiring.todos) == ["live"]  # revision 0 is not newer than 1
    wiring.hydrate([call_row("c1"), result_row("c1", stored([], 4))])
    assert ids(wiring.todos) == [] and wiring.todos.revision == 4


def test_executor_result_round_trips_through_hydration() -> None:
    wiring = TodoWiring()
    call = ToolCall("todo_list", {"todos": [item("a", parent="b"), item("b")]}, "c9")
    result = asyncio.run(wiring.execute(call, object()))
    again = TodoWiring()
    again.hydrate([call_row("c9"), result_row("c9", result.text)])
    assert again.todos.read() == wiring.todos.read()
    assert again.todos.revision == wiring.todos.revision


# -- through the agent -------------------------------------------------------------------------


def test_the_list_survives_a_resume_through_the_store(scope: Scope, fake_clock: FakeClock) -> None:
    store = MemoryStore()
    registry = ToolRegistry(clock=fake_clock)
    todo.register_tools(registry)
    plan = [item("one", "first step", "in_progress"), item("two", "second step")]

    async def first() -> tuple[list[dict[str, str]], set[str]]:
        server = FakeModelServer(
            script_of(tool_batch(("todo_list", {"todos": plan})), text_turn("planned")),
            clock=fake_clock,
        )
        agent = await Agent.start(
            scope,
            fake_route(server),
            ["todo"],
            fake_options(server, registry=registry, store=store),
        )
        result = await run_driven(agent.run_turn("plan it"), fake_clock)
        assert result.exit_reason is ExitReason.COMPLETED
        assert agent.todo is not None
        listed = agent.todo.todos.read()
        names = {t["function"]["name"] for t in server.model_requests[0].body["tools"]}
        await agent.close()
        return listed, names

    async def second() -> list[dict[str, str]]:
        server = FakeModelServer(script_of(text_turn("resumed")), clock=fake_clock)
        agent = await Agent.start(
            scope,
            fake_route(server),
            ["todo"],
            fake_options(server, registry=registry, store=store),
        )
        assert agent.todo is not None and len(agent.todo.todos) == 0
        await run_driven(agent.run_turn("carry on"), fake_clock)
        listed = agent.todo.todos.read()
        await agent.close()
        return listed

    listed, names = asyncio.run(first())
    assert "todo_list" in names
    assert [t["id"] for t in listed] == ["one", "two"]
    assert asyncio.run(second()) == listed
