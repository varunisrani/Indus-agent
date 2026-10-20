"""P19-11: approval requests as editor permission cards (option ids, timeout, dismissal)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from acp.schema import PermissionOption, RequestPermissionRequest

from indus.acp.attached_host import AttachedHost
from indus.acp.permissions import CHOICE_OF, approval_options
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice
from indus.state import messages
from indus.state.registry import StoreHandle
from indus.tools.calls import ToolCall, ToolResult
from tests.contract.acp.harness import KINDS, Harness, hold, open_harness
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import confirm
from tests.unit.backends._aio import aio

NEW = {"mcpServers": []}
PROMPT = [{"type": "text", "text": "clean up"}]
ALL = list(ApprovalChoice)


def _pick(option_id: str) -> Any:
    def answer(_params: Mapping[str, Any]) -> dict[str, Any]:
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    return answer


def _dismiss(_params: Mapping[str, Any]) -> dict[str, Any]:
    return {"outcome": {"outcome": "cancelled"}}


async def _new(h: Harness, tmp_path: Path) -> str:
    return str((await h.call("session/new", {"cwd": str(tmp_path), **NEW}))["sessionId"])


def _tool_result(store: StoreHandle, sid: str) -> str:
    rows = [m for m in messages.get(store, sid) if m["role"] == "tool"]
    return str(rows[-1]["content"]) if rows else ""


def test_options_follow_the_choices_and_map_by_id() -> None:
    every = approval_options(ALL)
    assert [(o["optionId"], o["kind"]) for o in every] == [
        ("allow_once", "allow_once"),
        ("allow_session", "allow_always"),  # ACP has no session kind; the id carries it
        ("allow_always", "allow_always"),
        ("deny", "reject_once"),
        ("deny_always", "reject_always"),
    ]
    for option in every:
        PermissionOption.model_validate(option)
    once_only = approval_options([ApprovalChoice.ONCE, ApprovalChoice.DENY])
    assert [o["optionId"] for o in once_only] == ["allow_once", "deny"]
    assert {k: v.value for k, v in CHOICE_OF.items()} == {
        "allow_once": "once",
        "allow_session": "session",
        "allow_always": "always",
        "deny": "deny",
        "deny_always": "deny",
    }


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("option_id", "choice"),
    [
        ("allow_once", "once"),
        ("allow_session", "session"),
        ("allow_always", "always"),
        ("deny", "deny"),
        ("deny_always", "deny"),
    ],  # fmt: skip
)
@aio
async def test_the_answer_is_mapped_by_option_id(
    kind: str, option_id: str, choice: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (tool_batch(("confirm", {})), text_turn("ok"))
    script = {"session/request_permission": _pick(option_id)}
    async with open_harness(
        kind, scope, store, tmp_path, *turns, tools={"confirm": confirm}, script=script
    ) as h:
        sid = await _new(h, tmp_path)
        result = await h.call("session/prompt", {"sessionId": sid, "prompt": PROMPT})
        assert result["stopReason"] == "end_turn"
        assert _tool_result(store, sid) == f"approved: {choice}"
        [asked] = [r for r in h.pair.requests if r["method"] == "session/request_permission"]
        card = RequestPermissionRequest.model_validate(asked["params"])
        assert card.tool_call.kind == "execute" and card.tool_call.status == "pending"
        assert card.tool_call.title == "clean up: rm -r build"
        closed = [
            u
            for u in h.pair.updates(sid)
            if u.get("toolCallId") == card.tool_call.tool_call_id
            and u["sessionUpdate"] == "tool_call_update"
        ]
        assert closed[-1]["status"] == ("failed" if choice == "deny" else "completed")


@aio
async def test_a_dismissed_card_is_not_a_denial_when_attached(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (tool_batch(("confirm", {})), text_turn("ok"))
    script = {"session/request_permission": _dismiss}
    async with open_harness(
        "attached", scope, store, tmp_path, *turns, tools={"confirm": confirm}, script=script
    ) as h:
        sid = await _new(h, tmp_path)
        running = asyncio.ensure_future(
            h.call("session/prompt", {"sessionId": sid, "prompt": PROMPT})
        )
        bridge = h.agent.permissions
        await eventually(lambda: any(d.kind == "dismissed" for d in bridge.decisions), limit=10)
        held = h.agent.sessions.by_editor(sid)
        assert held is not None and held.offered == []  # released: a later snapshot re-shows it
        assert not running.done()  # the daemon's prompt is still open
        # Another viewer (a terminal) answers the same request.
        host = await h.backend()
        assert isinstance(host, AttachedHost)
        service = await host.start()
        found = service._session(sid)  # pyright: ignore[reportPrivateUsage]
        [request_id] = list(found.offers)
        generation = found.offers[request_id]
        assert await host.answer(
            sid, request_id, generation, {"kind": "approval", "choice": "once"}
        )
        result = await asyncio.wait_for(running, timeout=10)
        assert result["stopReason"] == "end_turn"
        assert _tool_result(store, sid) == "approved: once"


@aio
async def test_an_unknown_option_is_no_answer_when_attached(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (tool_batch(("confirm", {})), text_turn("ok"))
    script = {"session/request_permission": _pick("allow_forever")}
    async with open_harness(
        "attached", scope, store, tmp_path, *turns, tools={"confirm": confirm}, script=script
    ) as h:
        sid = await _new(h, tmp_path)
        running = asyncio.ensure_future(
            h.call("session/prompt", {"sessionId": sid, "prompt": PROMPT})
        )
        bridge = h.agent.permissions
        await eventually(lambda: any(d.kind == "unanswered" for d in bridge.decisions), limit=10)
        assert not running.done()
        await h.pair.notify("session/cancel", {"sessionId": sid})
        assert (await asyncio.wait_for(running, timeout=10))["stopReason"] == "cancelled"


@aio
async def test_a_dismissed_card_is_a_denial_in_local_mode(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    turns = (tool_batch(("confirm", {})), text_turn("ok"))
    script = {"session/request_permission": _dismiss}
    async with open_harness(
        "local", scope, store, tmp_path, *turns, tools={"confirm": confirm}, script=script
    ) as h:
        sid = await _new(h, tmp_path)
        result = await h.call("session/prompt", {"sessionId": sid, "prompt": PROMPT})
        assert result["stopReason"] == "end_turn"
        assert _tool_result(store, sid) == "approved: deny"


@aio
async def test_a_timed_out_card_reports_timeout_not_deny(
    scope: Scope, store: StoreHandle, tmp_path: Path, fake_clock: FakeClock
) -> None:
    turns = (tool_batch(("confirm", {})), text_turn("ok"))
    async with open_harness(
        "local", scope, store, tmp_path, *turns, tools={"confirm": confirm},
        script={"session/request_permission": hold},
    ) as h:  # fmt: skip
        sid = await _new(h, tmp_path)
        running = asyncio.ensure_future(
            h.call("session/prompt", {"sessionId": sid, "prompt": PROMPT})
        )
        await eventually(lambda: bool(h.pair.requests), limit=10)
        await h.pair.settle()
        fake_clock.advance(300.0)  # approvals.timeout (default 300 s)
        bridge = h.agent.permissions
        await eventually(lambda: [d.kind for d in bridge.decisions] == ["timeout"], limit=10)
        assert not running.done()  # nothing was answered: the agent's own deadline decides
        card = h.pair.requests[0]["params"]["toolCall"]["toolCallId"]
        await eventually(
            lambda: any(
                u.get("toolCallId") == card and u.get("status") == "failed"
                for u in h.pair.updates(sid)
            ),
            limit=5,
        )
        await h.pair.notify("session/cancel", {"sessionId": sid})
        assert (await asyncio.wait_for(running, timeout=10))["stopReason"] == "cancelled"
        assert "deny" not in _tool_result(store, sid)


async def write_file(call: ToolCall, state: TurnState) -> ToolResult:
    """An inline ``write_file`` that asks the way the real one does (``write <path>``)."""
    broker = state.control.broker
    assert broker is not None
    path = str(call.args.get("path"))
    payload = {"command": f"write {path}", "description": "write outside the workspace",
               "choices": ["once", "deny"]}  # fmt: skip
    try:
        answer = await broker.ask("approval", payload, turn_id=state.turn_id)
    except RequestNotAnswered as exc:
        return ToolResult(f"not approved: {exc}", is_error=True)
    assert isinstance(answer, ApprovalAnswer)
    return ToolResult(f"write {answer.choice.value}")


@aio
async def test_a_file_write_card_shows_the_diff(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    target = str(tmp_path / "notes.txt")
    turns = (tool_batch(("write_file", {"path": target, "content": "hello\n"})), text_turn("ok"))
    script = {"session/request_permission": _pick("allow_once")}
    async with open_harness(
        "local", scope, store, tmp_path, *turns, tools={"write_file": write_file}, script=script
    ) as h:
        sid = await _new(h, tmp_path)
        await h.call("session/prompt", {"sessionId": sid, "prompt": PROMPT})
        [asked] = [r for r in h.pair.requests if r["method"] == "session/request_permission"]
        card = RequestPermissionRequest.model_validate(asked["params"])
        assert card.tool_call.kind == "edit"
        assert card.tool_call.title == f"Approve edit: {target}"
        diff = asked["params"]["toolCall"]["content"][0]
        assert diff == {"type": "diff", "path": target, "newText": "hello\n"}
        assert [o["optionId"] for o in asked["params"]["options"]] == ["allow_once", "deny"]
        assert _tool_result(store, sid) == "write once"
