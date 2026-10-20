"""TEST DOUBLE: a stand-in for the TUI child of the dashboard's Chat tab (P18-07..09).

Run as ``python -m tests.fakes.pty.fake_tui [mode]`` with the repository root on ``PYTHONPATH``.
On start it puts its terminal in raw mode (when it has one), prints ``ready <cols>x<rows>``, writes
``FAKE_TUI_SESSION`` into the channel's active-session file when both are set, and publishes
``FAKE_TUI_PUBLISH`` numbered events to ``INDUS_TUI_SIDECAR_URL`` (offering the internal credential
as the dashboard's ``indus-dashboard-internal.<value>`` subprotocol, never in the URL). Then, per
mode:

- ``echo`` (default): every input chunk is answered ``got:<hex>``; ``size?`` prints the size;
  ``quit`` exits 0;
- ``deaf``: never reads its input (a child that stopped taking keys);
- ``helper``: starts a helper process in the same process group that ignores SIGHUP and
  SIGTERM, prints ``helper <pid>`` and then echoes.

Nothing here is product code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys

from indus.service.ws_auth import dashboard_offer
from tests.fakes.pty.ws_wire import WireClient


def _say(text: str) -> None:
    os.write(1, (text + "\r\n").encode())


def _size() -> str:
    try:
        cols, rows = os.get_terminal_size(0)
    except OSError:
        return "nosize"
    return f"{cols}x{rows}"


def _raw() -> None:
    if not os.isatty(0):
        return
    import tty

    with contextlib.suppress(Exception):
        tty.setraw(0)


def _remember_session() -> None:
    session = os.environ.get("FAKE_TUI_SESSION")
    path = os.environ.get("INDUS_TUI_DASHBOARD_SESSION_FILE") or os.environ.get(
        "INDUS_TUI_ACTIVE_SESSION_FILE"
    )
    if session and path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"session_id": session}, handle)


async def _publish(count: int) -> None:
    url = os.environ["INDUS_TUI_SIDECAR_URL"]
    credential = os.environ.get("INDUS_TUI_SIDECAR_CREDENTIAL", "")
    client = await WireClient.connect(url, dashboard_offer("internal", credential))
    for number in range(1, count + 1):
        await client.send_text(json.dumps({"method": "event", "params": {"n": number}}))
    await client.close()


def _helper() -> None:
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(0.2)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    _say(f"helper {child.pid}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    _raw()
    _say(f"ready {_size()}")
    _remember_session()
    count = int(os.environ.get("FAKE_TUI_PUBLISH") or 0)
    if count:
        asyncio.run(_publish(count))
        _say("published")
    if mode == "helper":
        _helper()
    if mode == "deaf":
        signal.pause()
        return 0
    while True:
        try:
            chunk = os.read(0, 4096)
        except OSError:
            return 0
        if not chunk:
            return 0
        _say("got:" + chunk.hex())
        if b"size?" in chunk:
            _say("size " + _size())
        if b"quit" in chunk:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
