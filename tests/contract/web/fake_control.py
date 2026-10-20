"""A fake daemon control socket for the dashboard's daemon routes (P18-15).

It speaks the control wire of ``service.gateway_control`` (one JSON line in, one out, then close)
on a real Unix socket. The socket lives in a short private temp folder and the profile home gets
the owner-only pointer file a real daemon writes when its home path is too long, so the client
code under test runs unchanged.

``verbs`` maps a verb to a handler ``params -> result`` (raise ``Refuse(code)`` to answer an
error). ``stop`` is answered by default: it records the call and, after answering, closes the
socket so the daemon reads as gone. ``calls`` lists ``(verb, params)`` in order.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from indus.service.gateway_control import POINTER_FILE

type Handler = Callable[[dict[str, Any]], dict[str, Any]]


class Refuse(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FakeControl:
    def __init__(self, home: Path, *, supports_stop: bool = True) -> None:
        self.home = home
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.folder = Path(tempfile.mkdtemp(prefix="ixc"))
        self.path = self.folder / "s"
        self.alive = True
        self.verbs: dict[str, Handler] = {
            "identify": lambda _p: {"pid": os.getpid(), "profile": "work", "code_version": "t"},
            "status": lambda _p: {"state": "running", "platforms": {}},
            "drain": lambda p: {"draining": bool(p.get("on", True)), "active_turns": 0},
        }
        if supports_stop:
            self.verbs["stop"] = lambda _p: {"stopping": True}
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        pointer = home / POINTER_FILE
        pointer.write_text(str(self.path), encoding="utf-8")
        pointer.chmod(0o600)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _answer(self, line: bytes) -> tuple[bytes, bool]:
        message = json.loads(line)
        verb = str(message.get("verb"))
        params = dict(message.get("params") or {})
        self.calls.append((verb, params))
        handler = self.verbs.get(verb)
        if handler is None:
            body = {"ok": False, "protocol": 1, "id": message.get("id"), "error": "unknown_verb"}
            return json.dumps(body).encode() + b"\n", False
        try:
            result = handler(params)
        except Refuse as exc:
            body = {"ok": False, "protocol": 1, "id": message.get("id"), "error": exc.code}
            return json.dumps(body).encode() + b"\n", False
        body = {"ok": True, "protocol": 1, "id": message.get("id"), "result": result}
        return json.dumps(body).encode() + b"\n", verb == "stop"

    def _serve(self) -> None:
        while self.alive:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                data = b""
                with contextlib.suppress(OSError):
                    while b"\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                if not data:
                    continue
                reply, stop = self._answer(data.partition(b"\n")[0])
                with contextlib.suppress(OSError):
                    conn.sendall(reply)
            if stop:
                self.close()

    def verbs_called(self) -> list[str]:
        return [verb for verb, _ in self.calls]

    def close(self) -> None:
        self.alive = False
        with contextlib.suppress(OSError):
            self._sock.close()
        with contextlib.suppress(OSError):
            self.path.unlink()

    def remove(self) -> None:
        self.close()
        (self.home / POINTER_FILE).unlink(missing_ok=True)
        shutil.rmtree(self.folder, ignore_errors=True)
