"""P25-06: the scripted Chat Completions model both apps talk to during a parity run."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.parity.scripted_model_server import (
    EDIT_NAME,
    QUESTION,
    RUN_COMMAND,
    SCRIPTS,
    ScriptedModelServer,
    ScriptError,
    Settings,
    confined,
    main,
)

FAST = Settings(frame_delay=0.002, keepalive=0.02, hang_cap=30.0)

RUN_TOOL = {
    "type": "function",
    "function": {
        "name": "terminal",
        "parameters": {
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string"}},
        },
    },
}
ASK_TOOL = {
    "type": "function",
    "function": {
        "name": "clarify",
        "parameters": {
            "type": "object",
            "required": ["questions"],
            "properties": {"questions": {"type": "array"}},
        },
    },
}
WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "parameters": {
            "type": "object",
            "required": ["path", "content"],
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        },
    },
}


class Log:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.changed = threading.Condition()

    def __call__(self, entry: dict[str, Any]) -> None:
        with self.changed:
            self.entries.append(entry)
            self.changed.notify_all()

    def wait_for(self, event: str, timeout: float = 10.0) -> dict[str, Any]:
        with self.changed:
            found = self.changed.wait_for(
                lambda: any(e["event"] == event for e in self.entries), timeout
            )
            assert found, f"no {event!r} in {[e['event'] for e in self.entries]}"
            return next(e for e in self.entries if e["event"] == event)


@pytest.fixture
def log() -> Log:
    return Log()


@pytest.fixture
def server(log: Log, tmp_path: Path) -> Iterator[ScriptedModelServer]:
    settings = Settings(
        edit_root=tmp_path / "work",
        frame_delay=FAST.frame_delay,
        keepalive=FAST.keepalive,
        hang_cap=FAST.hang_cap,
    )
    (tmp_path / "work").mkdir()
    with ScriptedModelServer(settings=settings, log=log) as running:
        yield running


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def frames(response: httpx.Response) -> list[str]:
    """The ``data:`` payloads of an SSE body, in order."""
    out: list[str] = []
    for block in response.text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(line[len("data: ") :])
    return out


def chat(server: ScriptedModelServer, model: str, messages: list[dict[str, Any]], **extra: Any):
    body = {"model": model, "messages": messages, "stream": True, **extra}
    return httpx.post(f"{server.base_url}/chat/completions", json=body, timeout=10)


def streamed(response: httpx.Response) -> tuple[str, dict[str, Any] | None, str | None]:
    """(text, tool call with joined arguments, finish reason) from a streamed answer."""
    text = ""
    call: dict[str, Any] | None = None
    finish: str | None = None
    for payload in frames(response):
        if payload == "[DONE]":
            continue
        chunk = json.loads(payload)
        for choice in chunk["choices"]:
            delta = choice["delta"]
            text += delta.get("content") or ""
            for piece in delta.get("tool_calls") or ():
                if "id" in piece:
                    call = {"id": piece["id"], "name": piece["function"]["name"], "arguments": ""}
                assert call is not None
                call["arguments"] += piece["function"].get("arguments", "")
            finish = choice["finish_reason"] or finish
    if call is not None:
        call["arguments"] = json.loads(call["arguments"])
    return text, call, finish


def test_stream_framing(server: ScriptedModelServer) -> None:
    response = chat(server, "echo", [user("hello parity")], stream_options={"include_usage": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    payloads = frames(response)
    assert payloads[-1] == "[DONE]"
    first = json.loads(payloads[0])
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"]["role"] == "assistant"
    usage = json.loads(payloads[-2])
    assert usage["choices"] == [] and usage["usage"]["total_tokens"] > 0
    text, call, finish = streamed(response)
    assert text == "You said: hello parity"
    assert call is None and finish == "stop"
    # the same history gives byte-identical frames (a second app sees the same stream)
    assert frames(chat(server, "echo", [user("hello parity")])) == [
        p for p in payloads if '"usage"' not in p
    ]


def test_tool_call_emission(server: ScriptedModelServer) -> None:
    response = chat(server, "approve_or_decline", [user("run something")], tools=[RUN_TOOL])
    text, call, finish = streamed(response)
    assert text == "I need to run one command first."
    assert call == {
        "id": "call_parity_run_1",
        "name": "terminal",
        "arguments": {"command": RUN_COMMAND},
    }
    assert finish == "tool_calls"
    tool_frames = [p for p in frames(response) if "tool_calls" in p]
    assert len(tool_frames) > 2  # the arguments arrive in fragments after the id/name frame


def asked_then_answered(result: str) -> list[dict[str, Any]]:
    return [
        user("run something"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_parity_run_1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_parity_run_1", "content": result},
    ]


def test_approve_or_decline_branches_on_the_tool_result(server: ScriptedModelServer) -> None:
    allowed = streamed(
        chat(
            server,
            "approve_or_decline",
            asked_then_answered('{"output": "HEAD is now at 1a2b3c4 parity seed"}'),
        )
    )
    assert allowed[0].startswith("The command ran. It printed:")
    assert "HEAD is now at" in allowed[0]
    denied = streamed(
        chat(
            server,
            "approve_or_decline",
            asked_then_answered("BLOCKED: the user denied this command. Do not retry it."),
        )
    )
    assert denied[0] == "Understood: the command was declined, so nothing ran."
    assert denied[1] is None


def test_clarify_asks_then_reads_the_answer_back(server: ScriptedModelServer) -> None:
    _text, call, _finish = streamed(
        chat(server, "clarify", [user("make a note")], tools=[ASK_TOOL])
    )
    assert call is not None and call["name"] == "clarify"
    assert call["arguments"] == {
        "questions": [{"question": QUESTION, "choices": ["blue", "green"]}]
    }
    # a tool that takes one question (another app's shape) gets the single-question form
    single = {
        "type": "function",
        "function": {
            "name": "ask",
            "parameters": {"type": "object", "required": ["question"], "properties": {}},
        },
    }
    _t, other, _f = streamed(chat(server, "clarify", [user("make a note")], tools=[single]))
    assert other is not None and other["arguments"]["question"] == QUESTION
    history = [
        user("make a note"),
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "call_q1", "content": '{"answers": ["green"]}'},
    ]
    text, call, _finish = streamed(chat(server, "clarify", history))
    assert call is None and "green" in text and text.startswith("Thanks. Your answer was:")


def test_edit_file_stays_inside_the_edit_root(server: ScriptedModelServer, tmp_path: Path) -> None:
    _text, call, _finish = streamed(
        chat(server, "edit_file", [user("write it")], tools=[WRITE_TOOL])
    )
    assert call is not None
    path = Path(call["arguments"]["path"])
    root = (tmp_path / "work").resolve()
    assert path.parent == root and path.name == EDIT_NAME
    for bad in ("../escape.md", "/etc/escape.md", ""):
        with pytest.raises(ScriptError, match="relative"):
            confined(root, bad)
    (root / "out").symlink_to(tmp_path)  # a link that leaves the root is refused too
    with pytest.raises(ScriptError, match="leaves"):
        confined(root, "out/escape.md")
    without_root = ScriptedModelServer(settings=Settings())
    assert without_root.settings.edit_path() == EDIT_NAME


def test_client_disconnect_cancels_the_stream(server: ScriptedModelServer, log: Log) -> None:
    for script in ("hang", "long_stream"):
        body = {"model": script, "messages": [user("go")], "stream": True}
        url = f"{server.base_url}/chat/completions"
        with httpx.Client(timeout=10) as client, client.stream("POST", url, json=body) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: ") and "content" in line:
                    break  # the first text frame arrived; walk away mid-stream
        cancelled = log.wait_for("cancelled")
        assert cancelled["script"] == script
        log.entries.clear()


def test_unknown_script_is_a_400(server: ScriptedModelServer) -> None:
    response = chat(server, "no-such-script", [user("hi")])
    assert response.status_code == 400
    assert "unknown script" in response.json()["error"]["message"]
    prefixed = httpx.post(
        f"http://127.0.0.1:{server.port}/nope/v1/chat/completions",
        json={"model": "echo", "messages": [user("hi")]},
        timeout=10,
    )
    assert prefixed.status_code == 400
    broken = httpx.post(f"{server.base_url}/chat/completions", content=b"{not json", timeout=10)
    assert broken.status_code == 400


def test_url_prefix_picks_the_script_and_models_lists_them(server: ScriptedModelServer) -> None:
    body = {"model": "anything", "messages": [user("hi there")], "stream": True}
    response = httpx.post(f"{server.base_url_for('echo')}/chat/completions", json=body, timeout=10)
    assert streamed(response)[0] == "You said: hi there"
    listed = httpx.get(f"{server.base_url}/models", timeout=10).json()
    assert [m["id"] for m in listed["data"]] == list(SCRIPTS)


def test_non_streaming_body(server: ScriptedModelServer) -> None:
    body = {"model": "approve_or_decline", "messages": [user("x")], "tools": [RUN_TOOL]}
    answer = httpx.post(f"{server.base_url}/chat/completions", json=body, timeout=10).json()
    choice = answer["choices"][0]
    assert answer["object"] == "chat.completion" and choice["finish_reason"] == "tool_calls"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {
        "command": RUN_COMMAND
    }


def test_side_request_without_tools_gets_plain_text(server: ScriptedModelServer) -> None:
    for script in ("approve_or_decline", "clarify", "edit_file"):
        text, call, finish = streamed(chat(server, script, [user("title this chat")]))
        assert text == "Parity conversation" and call is None and finish == "stop"


def test_nothing_is_logged_without_a_log_sink(tmp_path: Path) -> None:
    with ScriptedModelServer(settings=FAST) as quiet:
        assert quiet.log is None
        assert streamed(chat(quiet, "echo", [user("ok")]))[0] == "You said: ok"


def test_loopback_only_and_bad_pins() -> None:
    with pytest.raises(ScriptError, match="loopback"):
        ScriptedModelServer(host="0.0.0.0")
    with pytest.raises(SystemExit):
        main(["--default-script", "nope"])
