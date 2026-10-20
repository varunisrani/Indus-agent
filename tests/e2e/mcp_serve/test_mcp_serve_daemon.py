"""P19-14..17: ``indus mcp serve`` as a real process beside a real ``indus gateway run`` daemon.

The profile is a temp home (``tests.fakes.daemon.ProfileHome``) whose store holds one messaging
conversation, written with the real state API the way the gateway writes it. The MCP client is
this test, speaking newline-delimited JSON-RPC to the child's stdin and reading its stdout.

- With the daemon running: the tool list carries the two approval tools (the daemon answers
  ``approvals-open`` with its queue, D-16.30), ``conversations_list`` finds the conversation, and
  ``messages_send`` reaches the daemon (it answers; no platform is connected, so nothing is sent).
- With the daemon stopped: the approval tools are absent and ``messages_send`` answers
  ``{"error": "daemon not running"}``; the read tools still work (they never need the daemon).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import make_scope
from indus.mcp_serve.send import NO_DAEMON
from indus.mcp_serve.server import APPROVAL_TOOLS, TOOL_ORDER
from indus.state import gateway_routes, registry, sessions
from indus.state import messages as message_rows
from tests.fakes.daemon import ProfileHome
from tests.fakes.model.http_serve import serving
from tests.integration.service.conftest_hosts import steps_script

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX sockets and signals"),
    pytest.mark.slow,
]

TIMEOUT = 60.0
KEY = "agent:main:telegram:dm:4242"


def seed(profile: ProfileHome) -> None:
    scope = make_scope(profile.home, env=profile.env())
    store = registry.open_unshared(scope)
    try:
        sessions.create(store, "s-e2e", "telegram")
        gateway_routes.get_or_create(
            store,
            KEY,
            new_session_id=lambda: "s-e2e",
            platform="telegram",
            chat_type="dm",
            origin={"platform": "telegram", "chat_id": "4242", "user_name": "ada"},
            display_name="Ada",
        )
        message_rows.append(store, "s-e2e", {"role": "user", "content": "is the build green?"})
    finally:
        store.close()


class McpClient:
    """One ``indus mcp serve`` child: newline-delimited JSON-RPC over its stdio."""

    def __init__(self, profile: ProfileHome) -> None:
        self.proc: subprocess.Popen[str] = profile.spawn("mcp", "serve")
        self._next = 0
        ready = self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "0"},
            },
        )
        assert ready["result"]["serverInfo"]["name"] == "indus", ready
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, frame: dict[str, Any]) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(frame) + "\n")
        stdin.flush()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        stdout = self.proc.stdout
        assert stdout is not None
        while True:
            line = stdout.readline()
            if not line:
                raise AssertionError(f"mcp serve closed stdout before {method}: {self.close()}")
            frame = json.loads(line)  # stdout carries MCP frames only
            assert frame.get("jsonrpc") == "2.0", frame
            if frame.get("id") == ident:
                return frame

    def tools(self) -> list[str]:
        return [tool["name"] for tool in self.call("tools/list")["result"]["tools"]]

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        answer = self.call("tools/call", {"name": name, "arguments": arguments})
        result = answer["result"]
        assert not result.get("isError"), answer
        loaded = json.loads(result["content"][0]["text"])
        assert isinstance(loaded, dict)
        return loaded

    def close(self) -> str:
        """End the child (stdin closed); its stderr."""
        stdin = self.proc.stdin
        if stdin is not None and not stdin.closed:
            stdin.close()
        try:
            _out, err = self.proc.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _out, err = self.proc.communicate(timeout=10)
            raise AssertionError(f"mcp serve did not exit:\n{err}") from None
        return err


def test_mcp_serve_reads_the_store_and_sends_only_through_a_running_daemon(
    tmp_path: Path,
) -> None:
    with serving(steps_script([{"text": "unused"}])) as model:
        profile = ProfileHome(tmp_path / "mcp", model.port)
        seed(profile)
        try:
            daemon = profile.start_daemon()
            daemon.attachable()

            live = McpClient(profile)
            assert live.tools() == [*TOOL_ORDER, *APPROVAL_TOOLS]
            listed = live.tool("conversations_list", {})
            assert [c["session_key"] for c in listed["conversations"]] == [KEY]
            assert listed["conversations"][0]["display_name"] == "Ada"
            read = live.tool("messages_read", {"session_key": KEY})
            assert [m["content"] for m in read["messages"]] == ["is the build green?"]
            assert live.tool("permissions_list_open", {}) == {"count": 0, "approvals": []}
            reached = live.tool("messages_send", {"target": "telegram:4242", "message": "yes"})
            assert reached["sent"] is False, reached  # the daemon answered: nothing connected
            assert str(reached["error"]).startswith("platform_not_connected"), reached
            err = live.close()
            assert live.proc.returncode == 0, err
            assert "Traceback" not in err

            assert daemon.stop() == 0, daemon.log()

            alone = McpClient(profile)
            assert alone.tools() == list(TOOL_ORDER)
            assert alone.tool("conversations_list", {})["count"] == 1
            refused = alone.tool("messages_send", {"target": "telegram:4242", "message": "yes"})
            assert refused == {"error": NO_DAEMON}
            err = alone.close()
            assert alone.proc.returncode == 0, err
            assert "Traceback" not in err
        finally:
            profile.stop_all()
    assert profile.stray_processes() == []
