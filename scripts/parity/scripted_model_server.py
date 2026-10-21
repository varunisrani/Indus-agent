"""Scripted model server for parity runs (P25-06).

A loopback HTTP server that answers the OpenAI-compatible Chat Completions endpoint from named
scripts, so every app pointed at it (by a custom base URL) holds the same conversation. Written from
the public Chat Completions description; standard library only.

Script choice: the URL prefix ``/<script>/v1/…`` wins, then the request's ``model`` field, then
``--default-script``. Anything else is a 400. Scripts are stateless: each request is answered from
the messages it carries, so a retry or a second app sees the same answer for the same history.

As a program::

    python scripts/parity/scripted_model_server.py [--port 0] [--edit-root DIR] [--log -]

prints ``{"port": n}`` as its first stdout line and serves until SIGTERM, SIGINT or stdin closing.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

CREATED = 1_760_000_000  # a fixed timestamp keeps frames byte-stable between runs
EDIT_NAME = "parity-note.md"
EDIT_CONTENT = "Written by the parity edit script.\n"
RUN_COMMAND = (
    "git reset --hard HEAD"  # flagged as destructive by approval rules, harmless on a clean tree
)
QUESTION = "Which colour should the note use?"
CHOICES = ("blue", "green")
REFUSAL_WORDS = ("denied", "declined", "blocked", "not approved", "refused", "rejected")
LONG_WORDS = 600
WORDS_PER_FRAME = 3
KEEPALIVE_SECONDS = 0.25
HANG_CAP_SECONDS = 300.0
MAX_BODY = 8 * 1024 * 1024


class ScriptError(ValueError):
    """A request or configuration the scripts cannot serve."""


# -- the conversation as the client sent it ------------------------------------------------------


@dataclass(frozen=True)
class OfferedTool:
    name: str
    required: tuple[str, ...]
    properties: tuple[str, ...]


@dataclass(frozen=True)
class Conversation:
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[OfferedTool, ...]

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> Conversation:
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ScriptError("messages must be a non-empty list")
        messages = tuple(m for m in raw_messages if isinstance(m, dict))  # pyright: ignore[reportUnknownVariableType]
        tools: list[OfferedTool] = []
        for entry in body.get("tools") or ():
            if not isinstance(entry, dict):
                continue
            function: Any = entry.get("function")  # pyright: ignore[reportUnknownMemberType]
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):  # pyright: ignore[reportUnknownMemberType]
                continue
            params: Any = function.get("parameters")  # pyright: ignore[reportUnknownMemberType]
            params = params if isinstance(params, dict) else {}
            required: Any = params.get("required")  # pyright: ignore[reportUnknownMemberType]
            props: Any = params.get("properties")  # pyright: ignore[reportUnknownMemberType]
            tools.append(
                OfferedTool(
                    name=str(function["name"]),  # pyright: ignore[reportUnknownArgumentType]
                    required=tuple(str(r) for r in required) if isinstance(required, list) else (),  # pyright: ignore[reportUnknownVariableType]
                    properties=tuple(str(p) for p in props) if isinstance(props, dict) else (),  # pyright: ignore[reportUnknownVariableType]
                )
            )
        return cls(messages, tuple(tools))  # pyright: ignore[reportUnknownArgumentType]

    @property
    def last(self) -> Mapping[str, Any]:
        return self.messages[-1]

    @property
    def answered_tool(self) -> bool:
        """The client is sending back a tool result: the script's second half."""
        return self.last.get("role") == "tool"

    def last_text(self, role: str) -> str:
        for message in reversed(self.messages):
            if message.get("role") == role:
                return content_text(message.get("content"))
        return ""


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(part, dict) and isinstance(part.get("text"), str):  # pyright: ignore[reportUnknownMemberType]
                parts.append(str(part["text"]))  # pyright: ignore[reportUnknownArgumentType]
        return "".join(parts)
    return ""


# -- what a script answers -----------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedCall:
    name: str
    arguments: Mapping[str, Any]
    call_id: str


@dataclass(frozen=True)
class Plan:
    """One answer: text frames, at most one tool call, an optional hang, a frame pace."""

    chunks: tuple[str, ...] = ()
    call: PlannedCall | None = None
    hang: bool = False
    paced: bool = False

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self.call is not None else "stop"


@dataclass(frozen=True)
class Settings:
    edit_root: Path | None = None
    frame_delay: float = 0.01
    keepalive: float = KEEPALIVE_SECONDS
    hang_cap: float = HANG_CAP_SECONDS
    pinned_tools: Mapping[str, str] = field(default_factory=dict[str, str])

    def edit_path(self) -> str:
        if self.edit_root is None:
            return EDIT_NAME
        return str(confined(self.edit_root, EDIT_NAME))


def confined(root: Path, name: str) -> Path:
    """``name`` inside ``root``; a name that would leave it (or is absolute) is refused."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ScriptError(f"edit name {name!r} must be a plain relative path")
    base = root.resolve()
    target = (base / pure).resolve()
    if base != target and base not in target.parents:
        raise ScriptError(f"edit name {name!r} leaves {base}")
    return target


def words(text: str) -> tuple[str, ...]:
    """Split ``text`` into small frames of a few words each (keeping the spaces)."""
    tokens = text.split(" ")
    frames: list[str] = []
    for start in range(0, len(tokens), WORDS_PER_FRAME):
        piece = " ".join(tokens[start : start + WORDS_PER_FRAME])
        frames.append(piece if start + WORDS_PER_FRAME >= len(tokens) else piece + " ")
    return tuple(frames)


ROLE_MATCHERS: dict[str, Callable[[OfferedTool], bool]] = {
    "run_command": lambda t: "command" in t.required,
    "ask_user": lambda t: bool({"questions", "question"} & set(t.required)),
    "write_file": lambda t: {"path", "content"} <= set(t.required),
}


def pick_tool(conversation: Conversation, role: str, settings: Settings) -> OfferedTool | None:
    pinned = settings.pinned_tools.get(role)
    if pinned is not None:
        return next((t for t in conversation.tools if t.name == pinned), None)
    matches = [t for t in conversation.tools if ROLE_MATCHERS[role](t)]
    matches.sort(key=lambda t: (len(t.required), t.name))
    return matches[0] if matches else None


def side_answer() -> Plan:
    """A request without the tool a script needs (titles, summaries): short plain text."""
    return Plan(chunks=("Parity ", "conversation"))


def refused(result: str) -> bool:
    lowered = result.lower()
    return any(word in lowered for word in REFUSAL_WORDS)


def script_echo(conversation: Conversation, settings: Settings) -> Plan:
    return Plan(chunks=words("You said: " + conversation.last_text("user").strip()))


def script_approve_or_decline(conversation: Conversation, settings: Settings) -> Plan:
    if conversation.answered_tool:
        result = content_text(conversation.last.get("content"))
        if refused(result):
            return Plan(chunks=words("Understood: the command was declined, so nothing ran."))
        first = next((ln.strip() for ln in result.splitlines() if ln.strip()), "nothing")
        return Plan(chunks=words(f"The command ran. It printed: {first[:120]}"))
    tool = pick_tool(conversation, "run_command", settings)
    if tool is None:
        return side_answer()
    return Plan(
        chunks=("I need to run one ", "command first."),
        call=PlannedCall(tool.name, {"command": RUN_COMMAND}, "call_parity_run_1"),
    )


def script_clarify(conversation: Conversation, settings: Settings) -> Plan:
    if conversation.answered_tool:
        answer = " ".join(content_text(conversation.last.get("content")).split())[:160]
        return Plan(chunks=words(f"Thanks. Your answer was: {answer}"))
    tool = pick_tool(conversation, "ask_user", settings)
    if tool is None:
        return side_answer()
    if "questions" in tool.required:
        arguments: dict[str, Any] = {"questions": [{"question": QUESTION, "choices": [*CHOICES]}]}
    else:
        arguments = {"question": QUESTION, "choices": [*CHOICES]}
    return Plan(chunks=("One question first.",), call=PlannedCall(tool.name, arguments, "call_q1"))


def script_edit_file(conversation: Conversation, settings: Settings) -> Plan:
    if conversation.answered_tool:
        return Plan(chunks=words(f"The note is written to {EDIT_NAME}."))
    tool = pick_tool(conversation, "write_file", settings)
    if tool is None:
        return side_answer()
    arguments = {"path": settings.edit_path(), "content": EDIT_CONTENT}
    return Plan(
        chunks=("Writing the note.",),
        call=PlannedCall(tool.name, arguments, "call_parity_edit_1"),
    )


def script_long_stream(conversation: Conversation, settings: Settings) -> Plan:
    body = " ".join(f"word{n}" for n in range(1, LONG_WORDS + 1))
    return Plan(chunks=words("A long answer follows. " + body + " The end."), paced=True)


def script_hang(conversation: Conversation, settings: Settings) -> Plan:
    return Plan(chunks=("Working on it",), hang=True)


Script = Callable[[Conversation, Settings], Plan]
SCRIPTS: dict[str, Script] = {
    "echo": script_echo,
    "approve_or_decline": script_approve_or_decline,
    "clarify": script_clarify,
    "edit_file": script_edit_file,
    "long_stream": script_long_stream,
    "hang": script_hang,
}


# -- wire format ---------------------------------------------------------------------------------


def chunk_frame(model: str, delta: Mapping[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-parity",
        "object": "chat.completion.chunk",
        "created": CREATED,
        "model": model,
        "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish}],
    }


def usage_numbers(conversation: Conversation, plan: Plan) -> dict[str, int]:
    prompt = sum(len(content_text(m.get("content")).split()) for m in conversation.messages)
    completion = sum(len(c.split()) for c in plan.chunks) + (1 if plan.call else 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def argument_fragments(arguments: Mapping[str, Any], pieces: int = 3) -> list[str]:
    text = json.dumps(dict(arguments), separators=(",", ":"))
    size = max(1, -(-len(text) // pieces))
    return [text[i : i + size] for i in range(0, len(text), size)]


def stream_frames(model: str, plan: Plan) -> Iterator[dict[str, Any]]:
    """The ``chat.completion.chunk`` objects for ``plan`` (the handler adds the finish frame,
    usage and ``[DONE]``)."""
    yield chunk_frame(model, {"role": "assistant", "content": ""})
    for piece in plan.chunks:
        yield chunk_frame(model, {"content": piece})
    if plan.call is not None:
        head = {
            "index": 0,
            "id": plan.call.call_id,
            "type": "function",
            "function": {"name": plan.call.name, "arguments": ""},
        }
        yield chunk_frame(model, {"tool_calls": [head]})
        for fragment in argument_fragments(plan.call.arguments):
            yield chunk_frame(
                model, {"tool_calls": [{"index": 0, "function": {"arguments": fragment}}]}
            )


def whole_body(model: str, conversation: Conversation, plan: Plan) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "".join(plan.chunks) or None}
    if plan.call is not None:
        message["tool_calls"] = [
            {
                "id": plan.call.call_id,
                "type": "function",
                "function": {
                    "name": plan.call.name,
                    "arguments": json.dumps(dict(plan.call.arguments), separators=(",", ":")),
                },
            }
        ]
    return {
        "id": "chatcmpl-parity",
        "object": "chat.completion",
        "created": CREATED,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": plan.finish_reason}],
        "usage": usage_numbers(conversation, plan),
    }


def error_body(message: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}


def split_target(path: str) -> tuple[str | None, str]:
    """``/<script>/v1/chat/completions`` → (script, ``/v1/chat/completions``)."""
    clean = path.split("?", 1)[0]
    parts = [p for p in clean.split("/") if p]
    if len(parts) >= 2 and parts[1] == "v1" and parts[0] != "v1":
        return parts[0], "/" + "/".join(parts[1:])
    return None, "/" + "/".join(parts)


# -- the server ----------------------------------------------------------------------------------


class _Gone(Exception):
    """The client went away while we were writing."""


@dataclass
class ScriptedModelServer:
    """The scripted model on ``host:port``; ``start()`` serves on a daemon thread."""

    host: str = "127.0.0.1"
    port: int = 0
    default_script: str | None = None
    settings: Settings = field(default_factory=Settings)
    log: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        if not ipaddress.ip_address(self.host).is_loopback:
            raise ScriptError(f"the scripted model listens on loopback only, not {self.host}")
        if self.default_script is not None and self.default_script not in SCRIPTS:
            raise ScriptError(f"unknown default script {self.default_script!r}")
        if self.settings.edit_root is not None:
            confined(self.settings.edit_root, EDIT_NAME)
        self.stopping = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --

    def start(self) -> int:
        handler = type("ParityHandler", (_Handler,), {"owner": self})
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="parity-model", daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self.stopping.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def __enter__(self) -> ScriptedModelServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def base_url_for(self, script: str) -> str:
        return f"http://{self.host}:{self.port}/{script}/v1"

    def record(self, entry: dict[str, Any]) -> None:
        if self.log is not None:
            self.log(entry)

    # -- answering --

    def resolve(self, prefix: str | None, body: Mapping[str, Any]) -> str:
        if prefix is not None:
            if prefix not in SCRIPTS:
                raise ScriptError(f"unknown script {prefix!r}")
            return prefix
        model = body.get("model")
        if isinstance(model, str) and model in SCRIPTS:
            return model
        if self.default_script is not None:
            return self.default_script
        raise ScriptError(f"unknown script {model!r}; known: {', '.join(sorted(SCRIPTS))}")

    def plan_for(self, script: str, conversation: Conversation) -> Plan:
        return SCRIPTS[script](conversation, self.settings)


class _Handler(BaseHTTPRequestHandler):
    owner: ScriptedModelServer
    protocol_version = "HTTP/1.1"
    server_version = "IndusParityModel/1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return  # silence: frames go to --log only

    # -- plumbing --

    def _send_json(self, status: int, data: Mapping[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _chunk(self, data: bytes) -> None:
        try:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()
        except OSError as exc:  # broken pipe, reset, aborted: the client is gone
            raise _Gone from exc

    def _event(self, frame: Mapping[str, Any] | str) -> None:
        text = frame if isinstance(frame, str) else json.dumps(frame, separators=(",", ":"))
        self.owner.record({"event": "frame", "data": text})
        self._chunk(f"data: {text}\n\n".encode())

    # -- routes --

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        _prefix, rest = split_target(self.path)
        if rest.rstrip("/").endswith("/models"):
            data = [{"id": name, "object": "model", "owned_by": "parity"} for name in SCRIPTS]
            self._send_json(200, {"object": "list", "data": data})
            return
        self._send_json(404, error_body(f"no route {self.path}", "not_found"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        prefix, rest = split_target(self.path)
        if not rest.endswith("/chat/completions"):
            self._send_json(404, error_body(f"no route {self.path}", "not_found"))
            return
        size = int(self.headers.get("content-length") or 0)
        if size <= 0 or size > MAX_BODY:
            self._send_json(400, error_body("a JSON body is required", "bad_body"))
            return
        try:
            body: Any = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ScriptError("the body must be a JSON object")
            request: dict[str, Any] = body  # pyright: ignore[reportUnknownVariableType]
            script = self.owner.resolve(prefix, request)
            conversation = Conversation.from_body(request)
        except ValueError as exc:  # ScriptError and JSON errors alike
            self._send_json(400, error_body(str(exc), "unknown_script_or_bad_body"))
            return
        plan = self.owner.plan_for(script, conversation)
        model = str(request.get("model") or script)
        self.owner.record(
            {
                "event": "request",
                "script": script,
                "messages": len(conversation.messages),
                "last_role": conversation.last.get("role"),
                "stream": bool(request.get("stream")),
            }
        )
        if not request.get("stream"):
            self._send_json(200, whole_body(model, conversation, plan))
            self.owner.record({"event": "done", "script": script})
            return
        options: Any = request.get("stream_options")
        with_usage = isinstance(options, dict) and bool(options.get("include_usage"))  # pyright: ignore[reportUnknownMemberType]
        self._stream(script, model, conversation, plan, with_usage)

    def _stream(
        self,
        script: str,
        model: str,
        conversation: Conversation,
        plan: Plan,
        with_usage: bool,
    ) -> None:
        settings = self.owner.settings
        stopping = self.owner.stopping
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for frame in stream_frames(model, plan):
                if plan.paced and stopping.wait(settings.frame_delay):
                    return
                self._event(frame)
            if plan.hang:
                waited = 0.0
                while waited < settings.hang_cap and not stopping.wait(settings.keepalive):
                    waited += settings.keepalive
                    self._chunk(b": keep-alive\n\n")
            self._event(chunk_frame(model, {}, plan.finish_reason))
            if with_usage:
                usage = chunk_frame(model, {})
                usage["choices"] = []
                usage["usage"] = usage_numbers(conversation, plan)
                self._event(usage)
            self._event("[DONE]")
            self._chunk(b"")
            self.owner.record({"event": "done", "script": script})
        except _Gone:
            self.owner.record({"event": "cancelled", "script": script})


# -- program -------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scripted Chat Completions model for parity runs.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--default-script", choices=sorted(SCRIPTS))
    parser.add_argument("--edit-root", type=Path)
    parser.add_argument("--frame-delay-ms", type=int, default=10)
    parser.add_argument(
        "--tool",
        action="append",
        default=[],
        metavar="ROLE=NAME",
        help=f"pin the tool a role uses ({', '.join(sorted(ROLE_MATCHERS))})",
    )
    parser.add_argument(
        "--log", metavar="PATH", help="write one JSON line per frame ('-' = stderr)"
    )
    return parser.parse_args(list(argv))


def pinned(entries: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in entries:
        role, sep, name = entry.partition("=")
        if not sep or role not in ROLE_MATCHERS or not name:
            raise ScriptError(f"--tool wants ROLE=NAME with ROLE in {sorted(ROLE_MATCHERS)}")
        out[role] = name
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = Settings(
        edit_root=args.edit_root,
        frame_delay=max(0, args.frame_delay_ms) / 1000,
        pinned_tools=pinned(args.tool),
    )
    lock = threading.Lock()
    handle: Any = None
    if args.log:
        handle = sys.stderr if args.log == "-" else open(args.log, "a", encoding="utf-8")  # noqa: SIM115

    def sink(entry: dict[str, Any]) -> None:
        with lock:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()

    server = ScriptedModelServer(
        args.host,
        args.port,
        args.default_script,
        settings=settings,
        log=sink if handle is not None else None,
    )
    done = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: done.set())
    port = server.start()
    sys.stdout.write(json.dumps({"port": port}) + "\n")
    sys.stdout.flush()
    watcher = threading.Thread(target=lambda: (sys.stdin.read(), done.set()), daemon=True)
    watcher.start()
    with contextlib.suppress(KeyboardInterrupt):
        done.wait()
    server.stop()
    if handle is not None and handle is not sys.stderr:
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
