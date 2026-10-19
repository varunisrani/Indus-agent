"""``session_search`` (P09-25): discover, scroll, read, browse; lineage, hidden, redaction, cap."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from indus.core.persist import MemoryStore as InProcessStore
from indus.core.persist import StateStore
from indus.foundation.scope import Scope
from indus.learning import session_search as ss
from indus.learning.session_search import SessionSearch, SessionSearchWiring
from indus.state import registry, sessions
from indus.state.registry import StoreHandle
from indus.tools.calls import ToolCall
from tests.unit.learning._helpers_a import with_config
from tests.unit.state._helpers_a import chain, seed
from tests.unit.state._helpers_b import add_message, update_session

# Built at run time so no credential-shaped literal sits in the source.
SECRET = "gh" + "p_" + "A1b2C3d4" * 5


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def tool(store: StoreHandle, scope: Scope, current: str = "now", aux: Any = None) -> SessionSearch:
    return SessionSearch(store, scope, current=lambda: current, aux=aux)


def run(search: SessionSearch, **args: Any) -> dict[str, Any]:
    return asyncio.run(search.run(args))


def say(store: StoreHandle, sid: str, *texts: str, role: str = "user") -> list[int]:
    return [add_message(store, sid, role, text) for text in texts]


def test_discover_excludes_current_lineage(store: StoreHandle, scope: Scope) -> None:
    chain(store, ["root", "now"])  # "now" is the current conversation's latest segment
    say(store, "root", "we chose postgres for billing")
    say(store, "now", "postgres again today")
    seed(store, "old")
    say(store, "old", "postgres tuning notes")
    found = run(tool(store, scope), action="discover", query="postgres")
    assert [h["session_id"] for h in found["hits"]] == ["old"]
    every = run(tool(store, scope), action="discover", query="postgres", include_current=True)
    assert {h["session_id"] for h in every["hits"]} == {"root", "now", "old"}
    hit = found["hits"][0]
    assert "[[postgres]]" in hit["snippet"] and hit["role"] == "user" and hit["when"]


def test_hidden_sources(store: StoreHandle, scope: Scope) -> None:
    seed(store, "job", source="cron")
    seed(store, "child", source="subagent")
    seed(store, "chat")
    for sid in ("job", "child", "chat"):
        say(store, sid, "nightly backup report")
    shown = run(tool(store, scope), action="discover", query="backup")
    assert [h["session_id"] for h in shown["hits"]] == ["chat"]
    asked = run(tool(store, scope), action="discover", query="backup", include_hidden=True)
    assert {h["session_id"] for h in asked["hits"]} == {"job", "child", "chat"}
    refused = run(tool(store, scope), action="read", session_id="job")
    assert not refused["success"] and "include_hidden" in refused["error"]


def test_scroll_pages_without_repeats(store: StoreHandle, scope: Scope) -> None:
    seed(store, "s")
    say(store, "s", *[f"deploy step {i}" for i in range(7)])
    search = tool(store, scope)
    first = run(search, action="discover", query="deploy", limit=3, sort="newest")
    second = run(search, action="scroll")
    third = run(search, action="scroll")
    ids = [h["message_id"] for page in (first, second, third) for h in page["hits"]]
    assert len(ids) == 7 and len(set(ids)) == 7
    assert first["next_offset"] == 3 and "next_offset" not in third
    done = run(search, action="scroll")
    assert done["hits"] == []
    again = run(search, action="scroll", query="deploy", offset=5)
    assert len(again["hits"]) == 2


def test_scroll_needs_a_search(store: StoreHandle, scope: Scope) -> None:
    out = run(tool(store, scope), action="scroll")
    assert not out["success"] and "discover" in out["error"]


def test_read_window_bounded_and_tool_output_clipped(store: StoreHandle, scope: Scope) -> None:
    seed(store, "long")
    ids = say(store, "long", *[f"message {i}" for i in range(100)])
    add_message(store, "long", "tool", "x" * 5000, tool_name="terminal")
    search = tool(store, scope)
    centre = ids[50]
    out = run(search, action="read", session_id="long", message_id=centre, window=500)
    got = [m["message_id"] for m in out["messages"]]
    assert len(got) == ss.READ_MAX and centre in got
    assert got == sorted(got) and out["total_messages"] == 101
    tail = run(search, action="read", session_id="long", offset=95)
    tool_row = tail["messages"][-1]
    assert tool_row["role"] == "tool" and len(tool_row["text"]) <= ss.TOOL_CLIP
    assert tool_row["text"].endswith("[clipped]") and tool_row["tool_name"] == "terminal"
    missing = run(search, action="read", session_id="long", message_id=10**9)
    assert not missing["success"]


def test_read_refuses_current_and_unknown(store: StoreHandle, scope: Scope) -> None:
    seed(store, "now")
    say(store, "now", "hello")
    out = run(tool(store, scope), action="read", session_id="now")
    assert not out["success"] and "include_current" in out["error"]
    assert run(tool(store, scope), action="read", session_id="now", include_current=True)["success"]
    assert not run(tool(store, scope), action="read", session_id="ghost")["success"]


def test_secret_is_redacted(store: StoreHandle, scope: Scope) -> None:
    seed(store, "old")
    (mid,) = say(store, "old", f"the deploy token is {SECRET} keep it safe")
    update_session(store, "old", title=f"token {SECRET}")
    search = tool(store, scope)
    found = run(search, action="discover", query="deploy token")
    read = run(search, action="read", session_id="old", message_id=mid)
    browsed = run(search, action="browse")
    for body in (found, read, browsed):
        text = json.dumps(body)
        assert SECRET not in text
    assert "deploy token" in read["messages"][0]["text"]


def test_result_capped_with_continuation_hint(store: StoreHandle, scope: Scope) -> None:
    seed(store, "big")
    say(store, "big", *[("needle " + "word " * 900) for _ in range(30)])
    search = tool(store, scope)
    read = run(search, action="read", session_id="big", window=40)
    assert len(json.dumps(read)) <= ss.MAX_CHARS
    assert read["next_offset"] == len(read["messages"]) and "offset" in read["hint"]
    found = run(search, action="discover", query="needle", limit=25)
    assert len(json.dumps(found)) <= ss.MAX_CHARS
    assert "next_offset" in found


def test_browse_lists_recent_sessions(store: StoreHandle, scope: Scope, fake_clock: Any) -> None:
    for sid in ("a", "b", "now"):
        seed(store, sid)
        say(store, sid, f"first words of {sid}")
        fake_clock.advance(60)
    sessions.patch_model_config(store, "a", {})
    update_session(store, "b", title="Billing plan")
    out = run(tool(store, scope), action="browse")
    listed = {row["session_id"]: row for row in out["sessions"]}
    assert set(listed) == {"a", "b"}
    assert listed["b"]["title"] == "Billing plan"
    assert listed["a"]["preview"] == "first words of a"


def test_bad_action_and_blank_query(store: StoreHandle, scope: Scope) -> None:
    assert not run(tool(store, scope), action="peek")["success"]
    assert "query" in run(tool(store, scope), action="discover", query="  ")["error"]


class _Aux:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, Any]] = []

    async def call(self, task: str, messages: Any) -> Any:
        self.calls.append((task, messages))
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(text=self.reply)


def test_summarize_long_read(store: StoreHandle, scope: Scope) -> None:
    seed(store, "big")
    say(store, "big", *[("story " + "word " * 900) for _ in range(30)])
    on = with_config(scope, {"session_search": {"summarize": True}})
    aux = _Aux(f"They agreed on plan B; token {SECRET}.")
    out = run(tool(store, on, aux=aux), action="read", session_id="big", window=40)
    assert aux.calls and aux.calls[0][0] == "session_search"
    assert "plan B" in out["summary"] and SECRET not in out["summary"]
    assert out["condensed_messages"] == 30
    failing = _Aux(RuntimeError("down"))
    cut = run(tool(store, on, aux=failing), action="read", session_id="big", window=40)
    assert "messages" in cut and "hint" in cut
    off = run(tool(store, scope, aux=_Aux("unused")), action="read", session_id="big")
    assert "summary" not in off


def test_wiring_uses_the_runtime_store(store: StoreHandle, scope: Scope) -> None:
    seed(store, "old")
    say(store, "old", "kubernetes upgrade plan")
    wiring = SessionSearchWiring(scope)
    runtime = SimpleNamespace(store=StateStore(store), session_id="now")
    state: Any = SimpleNamespace(runtime=runtime)
    call = ToolCall("session_search", {"action": "discover", "query": "kubernetes"}, "c1")
    result = asyncio.run(wiring.execute(call, state))
    assert not result.is_error and json.loads(result.text)["hits"][0]["session_id"] == "old"
    runtime_mem: Any = SimpleNamespace(
        runtime=SimpleNamespace(store=InProcessStore(), session_id="x")
    )
    unavailable = asyncio.run(wiring.execute(call, runtime_mem))
    assert unavailable.is_error
