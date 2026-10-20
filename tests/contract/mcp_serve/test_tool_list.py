"""P19-18: the ``tools/list`` answer of ``indus mcp serve`` is a published contract.

Another agent's MCP client reads the tool names, descriptions and input schemas once and builds
its calls from them, so any change to them is a change for every client. The answer is compared
with ``tests/fixtures/mcp_serve/tool_list.json`` twice: without the daemon's approval queue (eight
tools) and with it (ten, D-16.30). On a difference the test writes what it got to ``actual.json``
in its temp folder; replace the snapshot with it only for an intended change, and say why.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.mcp_serve.approvals import Approvals
from indus.mcp_serve.server import APPROVAL_TOOLS, TOOL_ORDER, open_bridge
from tests.contract.mcp_serve.conftest import connected
from tests.unit.backends._aio import aio

REPO = Path(__file__).resolve().parents[3]
SNAPSHOT = REPO / "tests" / "fixtures" / "mcp_serve" / "tool_list.json"


class _Offered(Approvals):
    """A queue the probe found (the daemon side is ``test_approvals``'s)."""

    async def probe(self) -> bool:
        self.available = True
        return True


async def listed(scope: Scope, *, queue: bool) -> list[dict[str, Any]]:
    approvals = _Offered(scope) if queue else None
    bridge = await open_bridge(scope, start_events=False, approvals=approvals)
    try:
        async with connected(bridge) as client:
            tools = (await client.list_tools()).tools
    finally:
        await bridge.aclose()
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in tools
    ]


def render(lists: dict[str, list[dict[str, Any]]]) -> str:
    """The snapshot's layout: one compact line per tool, so a change shows as one changed tool."""
    out = ["{"]
    for n, (label, tools) in enumerate(sorted(lists.items())):
        out.append(f"  {json.dumps(label)}: [")
        rows = [f"    {json.dumps(tool, sort_keys=True)}" for tool in tools]
        out.append(",\n".join(rows))
        out.append("  ]" + ("," if n < len(lists) - 1 else ""))
    out.append("}")
    return "\n".join(out) + "\n"


@aio
async def test_the_tool_list_matches_the_snapshot(scope: Scope, tmp_path: Path) -> None:
    got = {
        "without_approval_queue": await listed(scope, queue=False),
        "with_approval_queue": await listed(scope, queue=True),
    }
    text = render(got)
    if text != SNAPSHOT.read_text(encoding="utf-8"):
        actual = tmp_path / "actual.json"
        actual.write_text(text, encoding="utf-8")
        pytest.fail(f"the MCP tool list differs from {SNAPSHOT.name}; what came is in {actual}")


@aio
async def test_every_tool_takes_an_object_and_names_its_required_arguments(scope: Scope) -> None:
    tools = await listed(scope, queue=True)
    assert [t["name"] for t in tools] == [*TOOL_ORDER, *APPROVAL_TOOLS]
    required = {t["name"]: sorted(t["inputSchema"].get("required", [])) for t in tools}
    assert required == {
        "conversations_list": [],
        "conversation_get": ["session_key"],
        "messages_read": ["session_key"],
        "attachments_fetch": ["message_id", "session_key"],
        "events_poll": [],
        "events_wait": [],
        "messages_send": ["message", "target"],
        "channels_list": [],
        "permissions_list_open": [],
        "permissions_respond": ["decision", "id"],
    }
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object", tool["name"]
        assert set(schema.get("required", [])) <= set(schema.get("properties", {})), tool["name"]
        assert tool["description"] and tool["description"].strip(), tool["name"]
