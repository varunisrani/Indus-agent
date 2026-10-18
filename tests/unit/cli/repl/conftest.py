"""A REPL on prompt_toolkit pipe input, driven from the test thread.

``rig`` starts ``Repl.run()`` on a background thread with a ``FakeHost``, a recording rich
console, prompt_toolkit's pipe input and dummy output, and the fake clock. The test types with
``rig.keys(...)``, hands feed items to the REPL with ``rig.feed(...)`` (run on the REPL's loop,
returning once handled) and waits for typed keys to be handled with ``rig.settle()``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.base import PipeInput
from prompt_toolkit.output import DummyOutput
from pydantic import TypeAdapter
from rich.console import Console

from indus.cli.repl.app import CommandHook, Repl, ReplOptions
from indus.foundation.scope import Scope
from indus.protocol.events import Event
from indus.protocol.server_requests import ServerRequest
from tests.fakes.clock import FakeClock
from tests.fakes.service import SESSION, FakeHost

WAIT = 5.0
STREAMS = Path(__file__).resolve().parents[3] / "fixtures" / "event_streams"

_EVENTS: TypeAdapter[Any] = TypeAdapter(Event)
_REQUESTS: TypeAdapter[Any] = TypeAdapter(ServerRequest)


def load_stream(name: str) -> list[Any]:
    """A recorded stream: event models, request models and ``"interrupt"`` markers."""
    items: list[Any] = []
    for line in (STREAMS / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
        data = json.loads(line)
        if "local" in data:
            items.append(data["local"])
        elif "type" in data:
            items.append(_EVENTS.validate_python(data))
        else:
            items.append(_REQUESTS.validate_python(data))
    return items


def recording_console(width: int = 80) -> Console:
    return Console(
        file=io.StringIO(),
        record=True,
        width=width,
        color_system=None,
        force_terminal=False,
        highlight=False,
    )


class Rig:
    def __init__(
        self,
        scope: Scope,
        clock: FakeClock,
        pipe: PipeInput,
        *,
        options: ReplOptions | None = None,
        commands: CommandHook | None = None,
        setup: Callable[[Repl], None] | None = None,
    ) -> None:
        self.host = FakeHost(SESSION)
        self.console = recording_console()
        self.clock = clock
        self.pipe = pipe
        self.repl = Repl(
            self.host,
            SESSION,
            scope=scope,
            options=options or ReplOptions(refresh_interval=60.0),
            commands=commands,
            console=self.console,
            input=pipe,
            output=DummyOutput(),
            clock=clock,
            use_patch_stdout=False,
        )
        if setup is not None:
            setup(self.repl)
        self.result: int | None = None
        self.thread = threading.Thread(target=self._run, name="repl-under-test", daemon=True)

    def _run(self) -> None:
        self.result = self.repl.run()

    def start(self) -> Rig:
        self.thread.start()
        assert self.repl.ready.wait(WAIT), "the REPL did not start"
        return self

    def keys(self, text: str) -> None:
        self.pipe.send_text(text)

    def call(self, fn: Callable[[], Any]) -> Any:
        return self.repl.post(fn).result(WAIT)

    def feed(self, *items: Any) -> None:
        for item in items:
            self.call(lambda item=item: self.repl.handle_item(item))

    def settle(self, hops: int = 3) -> None:
        """Return once keys already typed have been handled (a few loop turns later)."""
        done: concurrent.futures.Future[None] = concurrent.futures.Future()

        def hop(left: int) -> None:
            if left == 0:
                done.set_result(None)
            else:
                asyncio.get_running_loop().call_soon(hop, left - 1)

        self.repl.post(lambda: hop(hops))
        done.result(WAIT)

    def buffer_text(self) -> str:
        def read() -> str:
            app = self.repl.app
            return app.current_buffer.text if app is not None else ""

        return self.call(read)

    def text(self) -> str:
        return self.console.export_text(clear=False)

    def wait_exit(self) -> int | None:
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "the REPL did not exit"
        return self.result

    def stop(self) -> None:
        if self.thread.is_alive():
            self.repl.post(self.repl.request_exit)
            self.thread.join(WAIT)


@pytest.fixture
def pipe() -> Iterator[PipeInput]:
    with create_pipe_input() as created:
        yield created


@pytest.fixture
def make_rig(scope: Scope, fake_clock: FakeClock, pipe: PipeInput) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def build(**kwargs: Any) -> Rig:
        rig = Rig(scope, fake_clock, pipe, **kwargs)
        made.append(rig)
        return rig.start()

    yield build
    for rig in made:
        rig.stop()


@pytest.fixture
def rig(make_rig: Callable[..., Rig]) -> Rig:
    return make_rig()
