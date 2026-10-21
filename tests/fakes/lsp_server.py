"""A scriptable fake language server over stdio (P24-15). Run as a program:

    python tests/fakes/lsp_server.py SCRIPT.json

It speaks the LSP base protocol (``Content-Length`` frames) and finds diagnostics in a document's
text with one rule: every ``BAD(name)`` is an error "bad name" and every ``WARN(name)`` a warning,
on the line and columns where it stands. The script (a JSON object, every key optional) says how
it behaves:

- ``init``: ``"answer"`` (default), ``"silent"`` (never answer ``initialize``) or ``"crash"``
  (exit 3 on it);
- ``pull``: advertise ``diagnosticProvider`` and answer ``textDocument/diagnostic``;
- ``push`` (default true): publish diagnostics after each open/change;
- ``push_version`` (default true): put the document version in a publication;
- ``stale``: publish with the version before the current one (an old list);
- ``push_delay``: seconds to wait before publishing;
- ``pull_unsupported``: answer ``textDocument/diagnostic`` with MethodNotFound;
- ``content_modified``: answer that many ``textDocument/diagnostic`` requests with
  ContentModified before a real answer;
- ``ignore_exit`` / ``ignore_term``: stay alive after ``exit`` / ignore SIGTERM;
- ``die_on``: a method name that makes the server exit at once (code 9);
- ``stderr``: lines written to stderr at start;
- ``server_requests``: requests sent to the client after ``initialized``;
- ``log``: a file every received message (and the answers to server requests) is appended to,
  one JSON object per line;
- ``dump_env``: a file the server's environment is written to as JSON at start.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
from typing import Any

RULE = re.compile(r"(BAD|WARN)\(([^)]*)\)")
OUT = sys.stdout.buffer
LOCK = threading.Lock()


def send(message: dict[str, Any]) -> None:
    body = json.dumps(message).encode("utf-8")
    with LOCK:
        OUT.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        OUT.flush()


def read() -> dict[str, Any] | None:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode("ascii").strip()
        if not text:
            break
        name, _, value = text.partition(":")
        if name.lower() == "content-length":
            length = int(value)
    if length is None:
        return None
    return json.loads(sys.stdin.buffer.read(length))


def diagnostics(text: str) -> list[dict[str, Any]]:
    found = []
    for number, line in enumerate(text.split("\n")):
        for match in RULE.finditer(line):
            found.append(
                {
                    "range": {
                        "start": {"line": number, "character": match.start()},
                        "end": {"line": number, "character": match.end()},
                    },
                    "severity": 1 if match.group(1) == "BAD" else 2,
                    "source": "fake",
                    "message": f"{match.group(1).lower()} {match.group(2)}",
                }
            )
    return found


def main() -> int:
    script: dict[str, Any] = {}
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as handle:
            script = json.load(handle)
    log_path = script.get("log")

    def log(entry: dict[str, Any]) -> None:
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")

    if script.get("dump_env"):
        with open(script["dump_env"], "w", encoding="utf-8") as handle:
            json.dump(dict(os.environ), handle)
    if script.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for line in script.get("stderr", []):
        sys.stderr.write(line + "\n")
    sys.stderr.flush()

    docs: dict[str, tuple[int, str]] = {}
    modified_left = int(script.get("content_modified", 0))
    next_id = 1000

    def publish(uri: str) -> None:
        version, text = docs[uri]
        params: dict[str, Any] = {"uri": uri, "diagnostics": diagnostics(text)}
        if script.get("push_version", True):
            params["version"] = version - 1 if script.get("stale") else version
        delay = float(script.get("push_delay", 0))
        message = {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": params}
        if delay > 0:
            threading.Timer(delay, send, (message,)).start()
        else:
            send(message)

    while True:
        message = read()
        if message is None:
            return 0
        log(message)
        method = message.get("method")
        ident = message.get("id")
        params = message.get("params") or {}
        if method is None:  # an answer to one of our requests
            continue
        if method == script.get("die_on"):
            os._exit(9)
        if method == "initialize":
            mode = script.get("init", "answer")
            if mode == "silent":
                continue
            if mode == "crash":
                sys.stderr.write("fatal: cannot initialize\n")
                sys.stderr.flush()
                return 3
            caps: dict[str, Any] = {"textDocumentSync": 1}
            if script.get("pull") or script.get("pull_unsupported"):
                caps["diagnosticProvider"] = {"interFileDependencies": False}
            send({"jsonrpc": "2.0", "id": ident, "result": {"capabilities": caps}})
        elif method == "initialized":
            for request in script.get("server_requests", []):
                send({"jsonrpc": "2.0", "id": next_id, **request})
                next_id += 1
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": ident, "result": None})
        elif method == "exit":
            if not script.get("ignore_exit"):
                return 0
        elif method == "textDocument/didOpen":
            doc = params["textDocument"]
            docs[doc["uri"]] = (doc["version"], doc["text"])
            if script.get("push", True):
                publish(doc["uri"])
        elif method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            docs[uri] = (params["textDocument"]["version"], params["contentChanges"][-1]["text"])
            if script.get("push", True):
                publish(uri)
        elif method == "textDocument/didClose":
            docs.pop(params["textDocument"]["uri"], None)
        elif method == "textDocument/diagnostic":
            if script.get("pull_unsupported"):
                error = {"code": -32601, "message": "no pull here"}
                send({"jsonrpc": "2.0", "id": ident, "error": error})
            elif modified_left > 0:
                modified_left -= 1
                error = {"code": -32801, "message": "content modified"}
                send({"jsonrpc": "2.0", "id": ident, "error": error})
            else:
                uri = params["textDocument"]["uri"]
                items = diagnostics(docs.get(uri, (0, ""))[1])
                send({"jsonrpc": "2.0", "id": ident, "result": {"kind": "full", "items": items}})
        elif ident is not None:
            send({"jsonrpc": "2.0", "id": ident, "result": None})


if __name__ == "__main__":
    sys.exit(main())
