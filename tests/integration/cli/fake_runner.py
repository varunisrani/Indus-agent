"""Run the real ``indus`` entry point against the fake model (subprocess driver for tests).

Usage: ``python -m tests.integration.cli.fake_runner <indus args...>`` with
``INDUS_TEST_MODEL_SCRIPT`` naming a fake-model script (a bare name from
``tests/fixtures/model_scripts`` or a path). The process behaves exactly like ``indus`` (process
env, hard exit for ``-z``) except that the provider route and HTTP client come from the fake.

Optional: ``INDUS_TEST_REQUESTS`` — a file that gets one JSON line per model request body (written
before the request is answered, so it survives the hard exit); ``INDUS_TEST_MARKER`` — a file
created when the first answer frame is sent (tests wait for it before sending a signal).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from indus.cli.args import HostOverrides
from indus.cli.entry import main
from indus.foundation.clock import SystemClock
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, fake_route, load_script

SCRIPT_ENV = "INDUS_TEST_MODEL_SCRIPT"
REQUESTS_ENV = "INDUS_TEST_REQUESTS"
MARKER_ENV = "INDUS_TEST_MARKER"


def overrides_for(script_name: str) -> HostOverrides:
    marker = os.environ.get(MARKER_ENV, "")
    log_path = os.environ.get(REQUESTS_ENV, "")

    def on_frame(_turn: int, _frame: bytes) -> None:
        if marker and not os.path.exists(marker):
            Path(marker).write_text("streaming\n", encoding="utf-8")

    server = FakeModelServer(load_script(script_name), clock=SystemClock(), on_frame=on_frame)

    async def record(request: httpx.Request) -> None:
        if log_path and request.method == "POST":
            body = json.loads(await request.aread() or b"null")
            with open(log_path, "a", encoding="utf-8") as out:
                out.write(json.dumps(body) + "\n")

    def client(_route: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=server.transport, event_hooks={"request": [record]})

    return HostOverrides(route=fake_route(server), client_factory=client, list_models=any_window)


def run() -> int:
    name = os.environ.get(SCRIPT_ENV, "")
    if not name:
        sys.stderr.write(f"fake_runner: set {SCRIPT_ENV}\n")
        return 64
    return main(overrides=overrides_for(name))


if __name__ == "__main__":
    raise SystemExit(run())
