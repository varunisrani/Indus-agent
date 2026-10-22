"""P18-30: the dashboard's Chat tab and a classic CLI on one daemon session (M6, no browser).

The browser suite (``apps/web/e2e``) runs the same demo in Chromium; this test keeps the
multi-surface check in the Python gate. A real ``indus gateway run`` daemon and a real ``indus
dashboard`` serve a temp profile whose model is the scripted fake (``tests.fakes.web_stack``). The
``/api/pty`` socket is dialled the way the SPA dials it — the loopback token offered only as a
subprotocol, never in the URL — and starts the real TUI in a PTY, attached to the daemon on the
session a first ``indus chat -q`` made. The classic REPL (``indus -r``, in a pseudo-terminal) is
attached to the same session.

- a prompt typed into the dashboard's terminal is answered, and the turn streams in the CLI;
- a prompt typed in the CLI streams in the dashboard's terminal; the approval it raises is
  answered there and the CLI's card closes;
- the store holds one user row per prompt, all in the one session;
- the dashboard's token (handed over in ``INDUS_DASHBOARD_SESSION_TOKEN``, the desktop shell's
  way) reaches neither the PTY child's environment nor any URL;
- the dashboard runs with ``CI=true`` (as on a CI runner) and the TUI in its PTY still draws.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from indus.service.ws_auth import dashboard_offer
from tests.fakes.daemon import ROOT
from tests.fakes.pty.ws_wire import WireClient, WireClosed
from tests.fakes.web_stack import WebStack, plain

pytest.importorskip("pexpect")

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX pseudo-terminals, sockets and signals"),
    pytest.mark.slow,
    pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning"),
]

TIMEOUT = 90.0
CLOCK = SystemClock()  # the children run on real time
TUI = ROOT / "apps" / "tui"
ENTRY = TUI / "dist" / "entry.js"

APPROVAL_TURN = [
    {"text": ["Streaming ", "from the daemon. "]},
    {
        "tool_call": {
            "name": "terminal",
            "arguments": {"command": "find victim -delete"},
            "id": "c1",
        }
    },
    {"finish": "tool_calls"},
]


def _node_ok() -> bool:
    found = shutil.which("node")
    if found is None:
        return False
    out = subprocess.run([found, "--version"], capture_output=True, text=True, check=False)
    parts = re.match(r"v(\d+)\.(\d+)", out.stdout.strip())
    return parts is not None and (int(parts[1]), int(parts[2])) >= (22, 18)


@pytest.fixture(scope="module")
def renderer() -> Path:
    """The TUI bundle the PTY child runs (built once when missing)."""
    if not _node_ok():
        pytest.skip("needs Node 22.18 or newer")
    if not ENTRY.exists():
        vite = TUI / "node_modules" / ".bin" / "vite"
        if not vite.exists():
            pytest.skip("needs `pnpm install` (the TUI is not built)")
        built = subprocess.run(
            [str(vite), "build", "--ssr", "src/entry.tsx", "--outDir", "dist", "--emptyOutDir"],
            cwd=TUI,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert built.returncode == 0 and ENTRY.exists(), built.stdout + built.stderr
    return ENTRY


@pytest.fixture
def token() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def stack(tmp_path: Path, renderer: Path, token: str) -> Iterator[WebStack]:
    assert renderer.exists()
    spec = {
        "root": str(tmp_path / "m6"),
        "turns": [
            [{"text": "Hello first."}],
            [{"text": ["Dashboard ", "turn ", "answered."]}],
            APPROVAL_TURN,
            [{"text": ["Cleaned ", "up."]}],
        ],
        # CI=true as on a CI runner: the PTY child inherits it, and the TUI must still draw
        # (Ink turns non-interactive under CI unless told otherwise; wave 42 integration).
        "dashboard": {
            "host": "127.0.0.1",
            "env": {"INDUS_DASHBOARD_SESSION_TOKEN": token, "CI": "true"},
        },
    }
    web = WebStack(spec)
    try:
        web.start()
        yield web
    finally:
        assert web.close() == []


class Screen:
    """The PTY socket's output, read in the background and kept as plain text."""

    def __init__(self, client: WireClient) -> None:
        self.client = client
        self.raw = ""
        self.task = asyncio.get_running_loop().create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                frame = await self.client.recv(timeout=600)
                self.raw += frame if isinstance(frame, str) else frame.decode("utf-8", "replace")
        except WireClosed:
            return

    @property
    def text(self) -> str:
        return plain(self.raw)

    async def shows(self, needle: str, limit: float = TIMEOUT) -> None:
        async with asyncio.timeout(limit):
            while needle not in self.text:
                if self.task.done():
                    raise AssertionError(f"the PTY socket closed:\n{self.text[-3000:]}")
                await CLOCK.sleep(0.05)

    async def type(self, text: str) -> None:
        for char in text:
            await self.client.send_text(char)
            await CLOCK.sleep(0.01)
        await CLOCK.sleep(0.2)
        await self.client.send_text("\r")


def _user_rows(web: WebStack, text: str) -> list[list[object]]:
    escaped = text.replace("'", "''")
    return web.rows(
        f"SELECT session_id FROM messages WHERE role = 'user' AND content = '{escaped}'"
    )


def test_the_dashboard_chat_and_a_cli_share_one_daemon_session(stack: WebStack, token: str) -> None:
    opened = stack.handle({"op": "indus", "args": ["chat", "-q", "hello"]})
    assert opened["code"] == 0, opened["stderr"]
    found = re.search(r"session_id: (\S+)", str(opened["stderr"]))
    assert found is not None
    sid = found[1]
    assert stack.dashboard is not None and stack.profile is not None
    (stack.profile.work / "victim").mkdir()
    port = stack.dashboard.port

    async def run() -> None:
        url = f"ws://127.0.0.1:{port}/api/pty?channel=chat-m6&attach={secrets.token_hex(16)}&resume={sid}"
        assert token not in url
        client = await WireClient.connect(url, dashboard_offer("token", token))
        assert client.subprotocol == "indus-dashboard-v1"
        screen = Screen(client)
        await client.send_text("\x1b[RESIZE:110;40]")
        await screen.shows("Ask anything")

        # The PTY child is the real TUI; the token is not in its environment.
        children = stack.pty_children()
        assert len(children) == 1
        environ = Path(f"/proc/{children[0]}/environ").read_bytes()
        assert token.encode() not in environ

        cli = await asyncio.to_thread(stack.handle, {"op": "cli.open", "session": sid})
        assert cli == {}

        def cli_sees(pattern: str) -> None:
            seen = stack.handle({"op": "cli.expect", "pattern": pattern, "timeout": TIMEOUT})
            assert seen["matched"], f"the CLI never showed {pattern!r}:\n{seen['screen']}"

        # 1. Typed in the dashboard: answered there, streamed live in the CLI.
        await screen.type("from the dashboard")
        await screen.shows("Dashboard turn answered.")
        await asyncio.to_thread(cli_sees, "Dashboard turn answered")

        # 2. Typed in the CLI: streams in the dashboard; its approval is answered there.
        await asyncio.to_thread(stack.handle, {"op": "cli.send", "text": "please clean up"})
        await screen.shows("Streaming from the daemon.")
        await screen.shows("Needs your approval")
        await asyncio.to_thread(cli_sees, "Approval needed")
        await client.send_text("\r")  # the first choice: allow once
        await asyncio.to_thread(cli_sees, "question answered in another window")
        await asyncio.to_thread(cli_sees, "Cleaned up")
        await screen.shows("Cleaned up.")

        screen.task.cancel()

    asyncio.run(run())

    assert stack.profile is not None
    assert not (stack.profile.work / "victim").exists()  # approved once, ran once
    assert stack.model is not None and stack.model.model_turns == 4
    # One user row per prompt, whichever surface typed it, all in the one session.
    for prompt in ("hello", "from the dashboard", "please clean up"):
        assert _user_rows(stack, prompt) == [[sid]], prompt
    assert token not in stack.dashboard.output()
