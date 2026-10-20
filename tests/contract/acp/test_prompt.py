"""P19-09: ``session/prompt`` and ``session/cancel`` over both hosts (attached: the real daemon
endpoint in-loop; local: the in-process service), with the fake model."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError

from indus.acp.attached_host import AttachedHost
from indus.acp.prompts import cancel_admission
from indus.foundation.scope import Scope
from indus.state import messages
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import KINDS, Harness, open_harness
from tests.fakes.agent import eventually
from tests.fakes.model import Fail, Turn, text_turn, tool_batch
from tests.fakes.session_host import Holder
from tests.unit.backends._aio import aio

NEW = {"mcpServers": []}


def _text(words: str) -> dict[str, Any]:
    return {"type": "text", "text": words}


async def _new(h: Harness, tmp_path: Path) -> str:
    result = await h.call("session/new", {"cwd": str(tmp_path), **NEW})
    return str(result["sessionId"])


def _user_rows(store: StoreHandle, sid: str, text: str) -> int:
    return sum(1 for m in messages.get(store, sid) if m["role"] == "user" and m["content"] == text)


def _admissions(store: StoreHandle, sid: str) -> list[str]:
    with store.read() as conn:
        rows = conn.execute(
            "SELECT admission_id FROM runtime_admissions WHERE session_id = ? ORDER BY seq",
            (sid,),
        ).fetchall()
    return [str(r[0]) for r in rows]


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_a_prompt_streams_its_reply_before_the_answer(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness(kind, scope, store, tmp_path, text_turn("Hello ", "there.")) as h:
        sid = await _new(h, tmp_path)
        result = await h.call("session/prompt", {"sessionId": sid, "prompt": [_text("hi")]})
        assert result["stopReason"] == "end_turn"
        assert "usage" in result and result["usage"]["totalTokens"] >= 0
        await h.pair.settle()
        frames = h.pair.frames
        answer = h.pair.index_of_response("stopReason")[-1]
        chunks = [
            i
            for i, f in enumerate(frames)
            if f.get("method") == "session/update"
            and f["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        assert chunks and max(chunks) < answer  # every reply chunk precedes the answer
        text = "".join(frames[i]["params"]["update"]["content"]["text"] for i in chunks)
        assert text == "Hello there."
        assert _user_rows(store, sid, "hi") == 1


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_an_unknown_session_is_not_found(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness(kind, scope, store, tmp_path) as h:
        with pytest.raises(RequestError) as missing:
            await h.call("session/prompt", {"sessionId": "nope", "prompt": [_text("hi")]})
        assert missing.value.code == -32002


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_a_failed_turn_is_an_admitted_turn_failed_error(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    failing = Turn((Fail(400, '{"error": {"message": "bad request"}}'),))
    async with open_harness(kind, scope, store, tmp_path, failing) as h:
        sid = await _new(h, tmp_path)
        with pytest.raises(RequestError) as failed:
            await h.call("session/prompt", {"sessionId": sid, "prompt": [_text("go")]})
        assert (failed.value.data or {}).get("reason") == "admitted_turn_failed"


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_cancel_stops_this_editors_running_turn(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    turns = (tool_batch(("hold", {})), text_turn("never"))
    async with open_harness(kind, scope, store, tmp_path, *turns, tools={"hold": holder}) as h:
        sid = await _new(h, tmp_path)
        running = asyncio.ensure_future(
            h.call("session/prompt", {"sessionId": sid, "prompt": [_text("long job")]})
        )
        await eventually(lambda: holder.entered == 1, limit=10)
        await h.pair.notify("session/cancel", {"sessionId": sid})
        result = await asyncio.wait_for(running, timeout=10)
        assert result["stopReason"] == "cancelled"
        updates = h.pair.updates(sid)
        hold_calls = [
            u for u in updates if u.get("toolCallId") and u["sessionUpdate"] == "tool_call_update"
        ]
        assert hold_calls and hold_calls[-1]["status"] in ("completed", "failed")


@aio
async def test_a_lost_ack_and_the_same_prompt_reuse_one_admission(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("attached", scope, store, tmp_path, text_turn("done once")) as h:
        daemon = h.daemon
        assert daemon is not None
        sid = await _new(h, tmp_path)
        host = await h.backend()
        assert isinstance(host, AttachedHost)
        service = await host.start()
        daemon.rig.last.cut_reply_to("prompt.submit")
        prompt = {"sessionId": sid, "prompt": [_text("build it")]}
        with pytest.raises(RequestError) as lost:
            await h.call("session/prompt", prompt)
        assert (lost.value.data or {}).get("reason") == "outcome_unknown"
        await eventually(lambda: service.channel.state == "ready", limit=10)
        await eventually(lambda: _user_rows(store, sid, "build it") == 1, limit=10)
        result = await asyncio.wait_for(h.call("session/prompt", prompt), timeout=20)
        assert result["stopReason"] == "end_turn"
        assert _user_rows(store, sid, "build it") == 1  # one admission, one user row
        assert len(_admissions(store, sid)) == 1
        texts = [
            u["content"]["text"]
            for u in h.pair.updates(sid)
            if u["sessionUpdate"] == "agent_message_chunk"
        ]
        assert "".join(texts).count("done once") == 1  # the settled reply is shown once


@aio
async def test_a_different_prompt_retires_the_retained_identity(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (text_turn("first"), text_turn("second"))
    async with open_harness("attached", scope, store, tmp_path, *turns) as h:
        daemon = h.daemon
        assert daemon is not None
        sid = await _new(h, tmp_path)
        host = await h.backend()
        assert isinstance(host, AttachedHost)
        service = await host.start()
        daemon.rig.last.cut_reply_to("prompt.submit")
        with pytest.raises(RequestError):
            await h.call("session/prompt", {"sessionId": sid, "prompt": [_text("one")]})
        await eventually(lambda: service.channel.state == "ready", limit=10)
        await eventually(lambda: _user_rows(store, sid, "one") == 1, limit=10)
        await asyncio.wait_for(
            h.call("session/prompt", {"sessionId": sid, "prompt": [_text("two")]}), timeout=20
        )
        assert h.agent.prompts.peek(sid) is None
        assert len(_admissions(store, sid)) == 2


@aio
async def test_a_lost_turn_refuses_the_prompt_without_sending_it(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    async with open_harness("attached", scope, store, tmp_path, text_turn("first")) as h:
        daemon = h.daemon
        assert daemon is not None
        sid = await _new(h, tmp_path)
        await h.call("session/prompt", {"sessionId": sid, "prompt": [_text("hello")]})
        [admission] = _admissions(store, sid)
        store.write(
            lambda conn: conn.execute(
                "UPDATE runtime_admissions SET state = 'unknown', outcome = NULL "
                "WHERE admission_id = ?",
                (admission,),
            )
        )
        before = sum(1 for f in daemon.rig.last.sent if f.get("method") == "prompt.submit")
        with pytest.raises(RequestError) as refused:
            await h.call("session/prompt", {"sessionId": sid, "prompt": [_text("again")]})
        assert (refused.value.data or {}).get("reason") == "unknown_execution"
        assert "outcome is unknown" in str(refused.value)
        assert f"/discard {admission}" in str(refused.value)
        after = sum(1 for f in daemon.rig.last.sent if f.get("method") == "prompt.submit")
        assert after == before  # nothing was sent
        assert _user_rows(store, sid, "again") == 0


@aio
async def test_cancel_never_interrupts_a_successor_turn_from_another_surface(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    turns = (text_turn("mine"), tool_batch(("hold", {})), text_turn("theirs"))
    async with open_harness(
        "attached", scope, store, tmp_path, *turns, tools={"hold": holder}
    ) as h:
        sid = await _new(h, tmp_path)
        await h.call("session/prompt", {"sessionId": sid, "prompt": [_text("my turn")]})
        [mine] = _admissions(store, sid)
        host = await h.backend()
        assert isinstance(host, AttachedHost)
        service = await host.start()
        # Another surface (a terminal) starts the next turn in the same session.
        await service.channel.request("prompt.submit", {"session_id": sid, "text": "theirs"})
        await eventually(lambda: holder.entered == 1, limit=10)
        await h.pair.notify("session/cancel", {"sessionId": sid})  # this editor has nothing running
        held = h.agent.sessions.by_editor(sid)
        assert held is not None
        await cancel_admission(h.agent, held, mine)  # a stale cancel of this editor's old turn
        await h.pair.settle()
        assert not holder.gate.is_set()
        assert (await host.receipt(sid, mine)).state == "terminal"
        theirs = [a for a in _admissions(store, sid) if a != mine]
        assert (await host.receipt(sid, theirs[0])).state == "started"  # still running
        holder.gate.set()


@aio
async def test_local_prompts_of_one_session_run_one_after_another(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    holder = Holder()
    turns = (tool_batch(("hold", {})), text_turn("one"), text_turn("two"))
    async with open_harness("local", scope, store, tmp_path, *turns, tools={"hold": holder}) as h:
        sid = await _new(h, tmp_path)
        first = asyncio.ensure_future(
            h.call("session/prompt", {"sessionId": sid, "prompt": [_text("first")]})
        )
        await eventually(lambda: holder.entered == 1, limit=10)
        second = asyncio.ensure_future(
            h.call("session/prompt", {"sessionId": sid, "prompt": [_text("/queue second")]})
        )
        await h.pair.settle()
        assert not second.done()
        holder.gate.set()
        assert (await asyncio.wait_for(first, 10))["stopReason"] == "end_turn"
        assert (await asyncio.wait_for(second, 10))["stopReason"] == "end_turn"
        assert _user_rows(store, sid, "second") == 1
