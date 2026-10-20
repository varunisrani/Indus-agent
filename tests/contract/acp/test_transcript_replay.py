"""P19-18: an editor session replayed against both hosts gives the same frames, and the golden ones.

``tests/fixtures/acp/zed_session.jsonl`` is an editor session we wrote ourselves in the order Zed
sends it (initialize, new session, a prompt whose tool asks for approval, load, list, a second
prompt cancelled while its card is open). Each line is one step:

- ``request``: send a request and wait for its answer (``keep`` names a result field whose value
  later steps use as ``$SESSION``);
- ``start``: send a request without waiting (``finish`` waits for it);
- ``notify``: send a notification;
- ``answer``: queue the editor's answer to the next request of ``method`` the agent sends
  (``"hold"`` leaves the card open, as an editor whose user has not clicked yet);
- ``await_request``: wait until the agent has sent ``count`` requests of ``method``.

``$CWD`` is the project folder. The frames the editor received (responses, notifications and the
agent's own requests, in arrival order) are normalised — session, tool call, message and server
request ids, times, the temp folder and our version become placeholders — and compared with
``zed_session.golden.jsonl``; the local and the attached host must both give it. Every frame also
validates against the vendor schema snapshot (``test_schema_drift``).

On a difference the test writes what it got to ``actual.jsonl`` in its temp folder and names it in
the failure. Replace the golden with it only when the output changed on purpose, and say why in
the change. The transcript itself is never edited to make a test pass.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from indus import __version__
from indus.foundation.scope import Scope
from indus.state.registry import StoreHandle
from tests.contract.acp.harness import KINDS, Harness, open_harness
from tests.contract.acp.test_schema_drift import TAG
from tests.fakes.agent import eventually
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.session_host import confirm
from tests.unit.backends._aio import aio

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures" / "acp"
TRANSCRIPT = FIXTURES / "zed_session.jsonl"
GOLDEN = FIXTURES / "zed_session.golden.jsonl"

TURNS = (
    tool_batch(("confirm", {})),
    text_turn("Removed the build folder."),
    tool_batch(("confirm", {})),
    text_turn("never reached"),
)


def steps() -> list[dict[str, Any]]:
    lines = TRANSCRIPT.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _fill(value: Any, names: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return names.get(value, value)
    if isinstance(value, list):
        return [_fill(v, names) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, Mapping):
        return {k: _fill(v, names) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


async def _hold() -> Any:
    await asyncio.Event().wait()


async def _quiet(h: Harness) -> None:
    """Wait until the agent's notices that follow an answer (post-reply updates, usage reads)
    are on the wire, as an editor's next action would come after them in a recorded session;
    without this the order between those notices and the next answer depends on load."""
    await eventually(lambda: not h.agent._background, limit=30)  # pyright: ignore[reportPrivateUsage]
    await h.agent.projection.flush()
    await h.pair.settle()


async def replay(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> tuple[list[Any], dict[str, Any]]:
    """Run the transcript against one host: the frames the editor received, in order, and the
    method of every request the editor sent, by JSON-RPC id."""
    project = tmp_path / "project"
    project.mkdir()
    names: dict[str, str] = {"$CWD": str(project)}
    answers: dict[str, list[Any]] = {}

    def editor(method: str) -> Any:
        async def answer(_params: Mapping[str, Any]) -> Any:
            queued = answers.get(method) or []
            if not queued:
                raise AssertionError(f"the transcript has no answer for {method}")
            result = queued.pop(0)
            if result == "hold":
                return await _hold()
            return result

        return answer

    script = {"session/request_permission": editor("session/request_permission")}
    started: list[asyncio.Future[Any]] = []
    sent: dict[str, Any] = {}
    async with open_harness(
        kind, scope, store, tmp_path, *TURNS, tools={"confirm": confirm}, script=script
    ) as h:
        for step in steps():
            what = step["step"]
            if what == "answer":
                answers.setdefault(step["method"], []).append(step["result"])
            elif what == "request":
                sent[str(len(sent))] = step["method"]
                result = await asyncio.wait_for(
                    h.call(step["method"], _fill(step["params"], names)), timeout=30
                )
                if "keep" in step:
                    names["$SESSION"] = str(result[step["keep"]])
                await _quiet(h)
            elif what == "start":
                sent[str(len(sent))] = step["method"]
                call = h.call(step["method"], _fill(step["params"], names))
                started.append(asyncio.ensure_future(call))
            elif what == "notify":
                await h.pair.notify(step["method"], _fill(step["params"], names))
            elif what == "await_request":
                method, count = step["method"], int(step["count"])
                await eventually(
                    lambda m=method, c=count: (
                        sum(1 for r in h.pair.requests if r["method"] == m) >= c
                    ),
                    limit=30,
                )
            elif what == "finish":
                await asyncio.wait_for(asyncio.gather(*started), timeout=30)
                await _quiet(h)
            else:
                raise AssertionError(f"unknown transcript step {what!r}")
        await h.pair.settle()
        frames = list(h.pair.frames)
    return normalise(frames, names), sent


class _Ids:
    def __init__(self, label: str) -> None:
        self.label = label
        self.seen: dict[str, str] = {}

    def __call__(self, raw: str) -> str:
        if raw not in self.seen:
            self.seen[raw] = f"${self.label}-{len(self.seen) + 1}"
        return self.seen[raw]


_TIMES = frozenset({"updatedAt", "createdAt"})
_SERVER_REQUEST = re.compile(r"\bsrq-[0-9a-f]+\b")


def normalise(frames: list[Any], names: Mapping[str, str]) -> list[Any]:
    """Placeholders for what differs between runs (see the module notes)."""
    tools, messages, requests = _Ids("TOOL"), _Ids("MESSAGE"), _Ids("REQUEST")
    swaps = sorted(((v, k) for k, v in names.items()), key=lambda pair: -len(pair[0]))

    def text(value: str) -> str:
        for raw, label in swaps:
            value = value.replace(raw, label)
        return _SERVER_REQUEST.sub(lambda m: requests(m.group(0)), value)

    def walk(value: Any, key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {k: walk(v, k) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, list):
            return [walk(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(value, str):
            return value
        if key == "toolCallId":
            return tools(value)
        if key == "messageId":
            return messages(value)
        if key in _TIMES:
            return "$TIME"
        if key == "version" and value == __version__:
            return "$VERSION"
        return text(value)

    return [walk(frame) for frame in frames]


def _lines(frames: list[Any]) -> str:
    return "".join(json.dumps(frame, sort_keys=True) + "\n" for frame in frames)


# -- the vendor schema ----------------------------------------------------------------------------

ANSWERS = {
    "initialize": "InitializeResponse",
    "session/new": "NewSessionResponse",
    "session/load": "LoadSessionResponse",
    "session/list": "ListSessionsResponse",
    "session/prompt": "PromptResponse",
}
CALLS = {
    "session/update": "SessionNotification",
    "session/request_permission": "RequestPermissionRequest",
}


def _validator(definition: str) -> Draft202012Validator:
    path = FIXTURES / f"schema-{TAG}.unstable.json"  # the superset: fork is unstable at this tag
    schema = json.loads(path.read_text(encoding="utf-8"))
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"})


def schema_problems(frames: list[Any], sent: Mapping[str, Any]) -> list[str]:
    """Frames that do not match the vendor schema (answers by the method they answer)."""
    problems: list[str] = []
    checked = 0
    for index, frame in enumerate(frames):
        if "method" in frame:
            definition = CALLS.get(frame["method"])
            body = frame.get("params")
        else:
            definition = ANSWERS.get(sent.get(str(frame.get("id")), ""))
            body = frame.get("result")
        if definition is None or body is None:
            problems.append(f"frame {index} has no schema check: {json.dumps(frame)[:120]}")
            continue
        checked += 1
        for error in _validator(definition).iter_errors(body):
            problems.append(f"frame {index} ({definition}): {error.message}")
    if not checked:
        problems.append("no frame was checked")
    return problems


# -- tests ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@aio
async def test_the_transcript_gives_the_golden_frames_on_both_hosts(
    kind: str, scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    frames, _sent = await replay(kind, scope, store, tmp_path)
    got = _lines(frames)
    if got != GOLDEN.read_text(encoding="utf-8"):
        actual = tmp_path / "actual.jsonl"
        actual.write_text(got, encoding="utf-8")
        pytest.fail(f"{kind}: the frames differ from {GOLDEN.name}; what came is in {actual}")


@aio
async def test_every_frame_of_the_session_matches_the_vendor_schema(
    scope: Scope, store: StoreHandle, tmp_path: Path
) -> None:
    frames, sent = await replay("local", scope, store, tmp_path)
    assert schema_problems(frames, sent) == []
    kinds = {
        frame["params"]["update"]["sessionUpdate"]
        for frame in frames
        if frame.get("method") == "session/update"
    }
    assert {"tool_call", "tool_call_update", "agent_message_chunk", "user_message_chunk"} <= kinds
    # The check bites: a tool call with a kind the vendor does not define is reported.
    [first] = [f for f in frames if f.get("params", {}).get("update", {}).get("kind")][:1]
    bad = json.loads(json.dumps(first))
    bad["params"]["update"]["kind"] = "shell"
    assert len(schema_problems([bad], sent)) == 1


@pytest.mark.live
def test_a_headless_editor_runs_the_session() -> None:
    """The live lane: a real editor. The machines that run this suite have no headless Zed; the
    skip reason is the report."""
    import shutil

    if shutil.which("zed") is None:
        pytest.skip("no `zed` binary on PATH: the live editor run was not performed")
    pytest.skip("`zed` is present, but it has no headless agent-panel driver to script")
