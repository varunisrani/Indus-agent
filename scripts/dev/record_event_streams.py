"""Record protocol event streams for the REPL and TUI render tests (developer only).

Usage::

    uv run python scripts/dev/record_event_streams.py

Each scenario runs the real stack — ``InProcessHost``, ``SessionService``, the agent and its turn
loop — against the scripted fake model (``tests/fakes/model``), with an in-memory store and small
recording tools, and saves everything the session's feed delivered to
``tests/fixtures/event_streams/<scenario>.jsonl``: one JSON object per line, an event (it has a
``type``) or a server request (it has a ``kind``). A line ``{"local": "interrupt"}`` marks where
the recording client interrupted the turn.

Random ids are replaced so a re-recording only differs where behaviour changed: the session id by
``20260101_090000_rec001``, turn ids by ``turn_<n>``, request ids by ``sr_<n>``. No network is used.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indus.foundation.scope import Scope, make_scope  # noqa: E402
from indus.protocol.base import ProtocolModel  # noqa: E402
from indus.protocol.events import MessageDelta, TurnDone  # noqa: E402
from indus.protocol.server_requests import ApprovalRequest  # noqa: E402
from indus.safety.redaction import redact_output  # noqa: E402
from indus.service.host_inprocess import Feed, InProcessHost  # noqa: E402
from indus.service.session_service import SessionService  # noqa: E402
from indus.tools.registry import ToolRegistry  # noqa: E402
from tests.fakes.agent import fake_options  # noqa: E402
from tests.fakes.clock import FakeClock  # noqa: E402
from tests.fakes.model import (  # noqa: E402
    FakeModelServer,
    Finish,
    Script,
    Stall,
    Text,
    Turn,
    fake_route,
    script_of,
    text_turn,
    tool_batch,
)

OUT = ROOT / "tests" / "fixtures" / "event_streams"
WAIT = 10.0
SESSION = "20260101_090000_rec001"


def recording_tools() -> ToolRegistry:
    """``terminal`` (asks for approval when its command contains ``curl``) and ``read_file``."""
    tools = ToolRegistry()
    command_schema = {
        "description": "run a shell command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }

    async def terminal(args: Mapping[str, Any], broker: Any = None, turn_id: Any = None) -> str:
        command = str(args["command"])
        if "curl" in command and broker is not None:
            payload = {
                "command": redact_output(command, force=True),
                "description": "sends data to a remote host",
                "choices": ["once", "session", "always", "deny"],
            }
            answer = await broker.ask("approval", payload, timeout=300.0, turn_id=turn_id)
            if answer.choice == "deny":
                return "the person denied this command"
        return "hello from the recorder\n"

    read_schema = {
        "description": "read a text file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }

    async def read_file(args: Mapping[str, Any]) -> str:
        return f"1|contents of {args['path']}"

    tools.register("terminal", "recording", command_schema, terminal)
    tools.register("read_file", "recording", read_schema, read_file)
    return tools


class Normaliser:
    def __init__(self) -> None:
        self.maps: dict[str, dict[str, str]] = {"turn": {}, "request": {}}

    def _swap(self, kind: str, value: str, prefix: str) -> str:
        table = self.maps[kind]
        if value not in table:
            table[value] = f"{prefix}{len(table) + 1}"
        return table[value]

    def dump(self, item: ProtocolModel) -> dict[str, Any]:
        data = item.model_dump(mode="json", exclude_none=True)
        data["session_id"] = SESSION
        if isinstance(data.get("turn_id"), str):
            data["turn_id"] = self._swap("turn", data["turn_id"], "turn_")
        if isinstance(data.get("request_id"), str):
            data["request_id"] = self._swap("request", data["request_id"], "sr_")
        return data


def run_scenario(
    scope: Scope,
    clock: FakeClock,
    script: Script,
    prompt: str,
    on_item: Callable[[InProcessHost, str, int, object, list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    server = FakeModelServer(script, clock=clock)
    tools = recording_tools()

    def make() -> SessionService:
        return SessionService(
            scope,
            fake_route(server),
            toolsets=["recording"],
            options=fake_options(server, session_id=None, registry=tools),
        )

    lines: list[dict[str, Any]] = []
    names = Normaliser()
    with InProcessHost(make, scope=scope, call_timeout=WAIT) as host:
        sid = host.create().session_id
        feed: Feed
        feed, _ = host.feed(sid)
        generation = host.submit(sid, prompt).generation
        while True:
            item = feed.get(timeout=WAIT)
            assert isinstance(item, ProtocolModel)
            lines.append(names.dump(item))
            if on_item is not None:
                on_item(host, sid, generation, item, lines)
            if isinstance(item, TurnDone):
                break
    return lines


def interrupt_after_two_deltas() -> Callable[..., None]:
    seen = {"deltas": 0}

    def hook(
        host: InProcessHost, sid: str, generation: int, item: object, lines: list[dict[str, Any]]
    ) -> None:
        if isinstance(item, MessageDelta):
            seen["deltas"] += 1
            if seen["deltas"] == 2:
                lines.append({"local": "interrupt"})
                host.interrupt(sid, generation)

    return hook


def answer_approvals_once(
    host: InProcessHost, sid: str, generation: int, item: object, lines: list[dict[str, Any]]
) -> None:
    if isinstance(item, ApprovalRequest):
        host.answer_server_request(sid, item.request_id, {"kind": "approval", "choice": "once"})


def write(name: str, lines: list[dict[str, Any]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines)
    (OUT / f"{name}.jsonl").write_text(text, encoding="utf-8")
    print(f"wrote {name}.jsonl ({len(lines)} items)")


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
        scope = make_scope(home, env={"INDUS_HOME": str(home)}, clock=clock)

        write(
            "plain_answer",
            run_scenario(
                scope,
                clock,
                script_of(text_turn("Hello! ", "Here is a list:\n", "- one\n- **two**\n", "Done.")),
                "say hello",
            ),
        )
        write(
            "tool_trail",
            run_scenario(
                scope,
                clock,
                script_of(
                    tool_batch(("read_file", {"path": "notes.txt"})),
                    tool_batch(("terminal", {"command": "python hello.py"})),
                    text_turn("The script printed a greeting.\n", "All good."),
                ),
                "read the notes and run the script",
            ),
        )
        long_wait = Turn(
            (Text(("Here is the first line\n", "and a second line that ")), Stall(600))
        )
        write(
            "interrupt_mid_delta",
            run_scenario(
                scope,
                clock,
                script_of(long_wait, Turn((Finish("stop"),))),
                "write me a long story",
                interrupt_after_two_deltas(),
            ),
        )
        token = "rec" + "0123456789abcdefghijklmnop"
        command = f"curl -H 'Authorization: Bearer {token}' https://example.invalid/upload"
        write(
            "approval_request",
            run_scenario(
                scope,
                clock,
                script_of(
                    tool_batch(("terminal", {"command": command})),
                    text_turn("Uploaded."),
                ),
                "upload the report",
                answer_approvals_once,
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
