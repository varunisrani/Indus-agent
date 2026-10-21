"""P18-09 follow-up: the TUI's dashboard feed against the real ``/api/pub`` route.

Node runs the TUI's own feed module (``apps/tui/src/transport/dashboardFeed.ts``, through the
driver ``tests/fakes/pty/feed_driver.mjs``) with the environment ``/api/pty`` gives the TUI child:
the publish URL and the internal credential. The app is the real one (``chat_rig``), so the frames
pass the real WebSocket helper and fan-out to ``/api/events`` subscribers.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from tests.contract.web.chat_rig import chat_server
from tests.contract.web.test_events_fanout import until
from tests.fakes.pty.ws_wire import WireClient
from tests.unit.backends._aio import aio

REPO = Path(__file__).resolve().parents[3]
DRIVER = REPO / "tests" / "fakes" / "pty" / "feed_driver.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node to run the TUI's feed module")

EVENTS: list[dict[str, Any]] = [
    {"type": "session.info", "session_id": "s-1", "seq": 1, "model": "fake-model"},
    {"type": "transport.stderr", "line": "a bridge log line"},
    {"type": "message.delta", "session_id": "s-1", "seq": 2, "text": "Hel"},
    {"type": "gateway.ready", "protocol_version": 1},
    {"type": "message.delta", "session_id": "s-1", "seq": 3, "text": "lo"},
]


async def run_driver(env: dict[str, str]) -> tuple[int, str, str]:
    assert NODE is not None
    proc = await asyncio.create_subprocess_exec(
        NODE,
        str(DRIVER),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(REPO),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 60)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode or 0, out.decode(), err.decode()


def _env(url: str, credential: str, events: list[dict[str, Any]]) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "INDUS_TUI_SIDECAR_URL": url,
        "INDUS_TUI_SIDECAR_CREDENTIAL": credential,
        "FEED_EVENTS": json.dumps(events),
        "FEED_SETTLE_MS": "10000",
    }


@aio
async def test_the_tui_feed_reaches_the_channel_s_subscribers(scope: Scope) -> None:
    async with chat_server(scope) as server:
        base, fanout = server.base, server.chat.fanout
        sub = await WireClient.connect(f"{base}/api/events?channel=chat-7", server.offer("token"))
        other = await WireClient.connect(f"{base}/api/events?channel=chat-8", server.offer("token"))
        await until(lambda: fanout.channels() == {"chat-7": 1, "chat-8": 1})
        link = server.chat.pty.link
        assert link is not None
        url = link.publish_url("chat-7") or ""
        assert server.internal not in url
        code, out, err = await run_driver(_env(url, link.credential(), EVENTS))
        assert code == 0, err
        assert out.strip() == "open sent=3 leaked=false"
        frames = [json.loads(await sub.recv()) for _ in range(3)]
        assert frames == [EVENTS[0], EVENTS[2], EVENTS[4]]  # protocol events only, in order
        with pytest.raises(TimeoutError):
            await sub.recv(timeout=0.3)
        with pytest.raises(TimeoutError):
            await other.recv(timeout=0.3)  # another channel's feed stays quiet
        assert server.internal not in out + err
        await sub.close()
        await other.close()


@aio
async def test_a_refused_credential_stops_the_feed_at_once(scope: Scope) -> None:
    async with chat_server(scope) as server:
        base, fanout = server.base, server.chat.fanout
        sub = await WireClient.connect(f"{base}/api/events?channel=chat-7", server.offer("token"))
        await until(lambda: fanout.channels() == {"chat-7": 1})
        link = server.chat.pty.link
        assert link is not None
        url = link.publish_url("chat-7") or ""
        wrong = "not" + "-the-" + "credential"
        env = {**_env(url, wrong, EVENTS), "FEED_WAIT_STATE": "stopped"}
        code, out, err = await run_driver(env)
        assert code == 0, err
        # The upgrade is accepted and closed 4401 at once, so the frames handed over die with the
        # socket; the feed stops for good (no redial).
        assert out.startswith("stopped sent=") and out.strip().endswith("leaked=false")
        with pytest.raises(TimeoutError):
            await sub.recv(timeout=0.3)
        assert wrong not in out + err
        await sub.close()


@aio
async def test_without_the_dashboard_s_variables_there_is_no_feed(scope: Scope) -> None:
    del scope
    env = {"PATH": os.environ.get("PATH", os.defpath), "FEED_EVENTS": json.dumps(EVENTS)}
    code, out, _err = await run_driver(env)
    assert (code, out.strip()) == (3, "no-plan")
