"""P19-12: slash commands typed in the editor (routes, refusals as replies, prompt text)."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.acp.commands import BRANCH_REFUSED, advertised, command_of
from indus.foundation.scope import Scope
from indus.state import messages, sessions
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import KINDS, Harness, open_harness
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn
from tests.unit.backends._aio import aio


async def _new(h: Harness, tmp_path: Path) -> str:
    return str((await h.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}))["sessionId"])


async def _say(h: Harness, sid: str, text: str) -> str:
    """Send ``text`` as a prompt; the agent text it produced."""
    before = len(h.pair.updates(sid))
    result = await h.call(
        "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}
    )
    assert result["stopReason"] == "end_turn"
    return "".join(
        u["content"]["text"]
        for u in h.pair.updates(sid)[before:]
        if u["sessionUpdate"] == "agent_message_chunk"
    )


def _users(store: StoreHandle, sid: str) -> list[str]:
    return [str(m["content"]) for m in messages.get(store, sid) if m["role"] == "user"]


def test_only_registered_words_are_commands() -> None:
    assert command_of("/title  My work") == ("title", "My work")
    assert command_of("/compact") == ("compress", "")
    assert command_of("/etc/hosts is wrong") is None
    assert command_of("/tmp/x") is None
    assert command_of("/frobnicate now") is None
    assert command_of("plain text") is None
    assert command_of("/") is None


def test_the_editor_is_offered_editor_commands() -> None:
    names = [entry["name"] for entry in advertised()]
    for wanted in (
        "help",
        "title",
        "model",
        "compress",
        "undo",
        "retry",
        "steer",
        "queue",
        "tools",
    ):
        assert wanted in names
    for hidden in ("branch", "quit", "history", "config", "approve"):
        assert hidden not in names
    assert len(names) == len(set(names))


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_branch_is_refused_and_unknown_words_are_prompt_text(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (text_turn("about foo"), text_turn("hosts answer"))
    async with open_harness(kind, scope, store, tmp_path, *turns) as h:
        sid = await _new(h, tmp_path)
        assert BRANCH_REFUSED in await _say(h, sid, "/branch")
        assert _users(store, sid) == []  # nothing reached the model
        assert await _say(h, sid, "/foo bar") == "about foo"
        assert await _say(h, sid, "/etc/hosts is wrong") == "hosts answer"
        assert _users(store, sid) == ["/foo bar", "/etc/hosts is wrong"]


@aio
async def test_title_help_tools_and_steer_in_local_mode(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path, text_turn("steered reply")) as h:
        sid = await _new(h, tmp_path)
        assert "/title [name]" in await _say(h, sid, "/help")
        assert await _say(h, sid, "/title Release prep") == "Session title set: Release prep\n"
        row = sessions.get(store, sid)
        assert row is not None and row["title"] == "Release prep"
        assert {"sessionUpdate": "session_info_update", "title": "Release prep"} in h.pair.updates(
            sid
        )
        assert "Title: Release prep" in await _say(h, sid, "/title")
        assert "tools" in await _say(h, sid, "/tools")
        assert "Usage: /steer" in await _say(h, sid, "/steer")
        # Nothing is running: the guidance runs as a prompt.
        assert await _say(h, sid, "/steer check the tests") == "steered reply"
        assert _users(store, sid) == ["check the tests"]


@aio
async def test_undo_and_retry_take_turns_back(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (text_turn("one"), text_turn("two"), text_turn("two again"))
    async with open_harness("local", scope, store, tmp_path, *turns) as h:
        sid = await _new(h, tmp_path)
        assert "nothing to undo" in await _say(h, sid, "/undo")
        await _say(h, sid, "first")
        await _say(h, sid, "second")
        assert "Usage: /undo" in await _say(h, sid, "/undo x")
        assert await _say(h, sid, "/retry") == "two again"
        assert _users(store, sid) == ["first", "second"]
        reply = await _say(h, sid, "/undo")
        assert reply.startswith("Took back 1 turn") and "Removed message: second" in reply
        assert _users(store, sid) == ["first"]


@aio
async def test_reset_starts_the_session_over(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path, text_turn("hello")) as h:
        sid = await _new(h, tmp_path)
        await _say(h, sid, "hi")
        assert "cleared" in await _say(h, sid, "/new")
        assert messages.get(store, sid) == []


@aio
async def test_attached_commands_without_a_daemon_verb_answer_as_a_reply(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("attached", scope, store, tmp_path) as h:
        sid = await _new(h, tmp_path)
        reply = await _say(h, sid, "/usage")
        assert "Model calls:" in reply  # answered by the daemon (session.mutate command)
        assert "Session title set: Something" in await _say(h, sid, "/title Something")


@aio
async def test_the_commands_update_follows_session_new(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("local", scope, store, tmp_path) as h:
        sid = await _new(h, tmp_path)
        await eventually(lambda: "available_commands_update" in h.pair.kinds(sid), limit=5)
        [update] = [
            u for u in h.pair.updates(sid) if u["sessionUpdate"] == "available_commands_update"
        ]
        names = {c["name"] for c in update["availableCommands"]}
        assert {"title", "model", "help"} <= names and "branch" not in names
