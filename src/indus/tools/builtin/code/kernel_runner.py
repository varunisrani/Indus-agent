# pyright: strict
"""The program a local session kernel runs (P10-10), kept as text and written to its staging dir.

Requests arrive on an inherited pipe (``INDUS_KERNEL_IN_FD``), one JSON line ``{"id", "code"}``
each; a listener thread queues them and ends the process the moment the pipe closes, which is
also how the kernel notices that Indus is gone, even in the middle of a cell. Each cell runs in
one namespace kept for the kernel's life, with ``sys.stdout``/``sys.stderr`` captured (each cut
to ``INDUS_KERNEL_CAPTURE`` characters); the reply goes out as one JSON line on a second pipe
(``INDUS_KERNEL_OUT_FD``). Output written straight to file descriptors 1 and 2 (child processes)
reaches Indus on the real stdout/stderr pipes. With no request for ``INDUS_KERNEL_IDLE_S``
seconds the kernel exits by itself; a cell that calls ``sys.exit`` ends it after its reply.
"""

from __future__ import annotations

SOURCE = '''\
"""Indus Agent session kernel: runs execute_code cells in one long-lived namespace."""
import contextlib
import io
import json
import linecache
import os
import queue
import sys
import threading
import traceback

_IN = int(os.environ.pop("INDUS_KERNEL_IN_FD"))
_OUT = int(os.environ.pop("INDUS_KERNEL_OUT_FD"))
_IDLE_S = float(os.environ.pop("INDUS_KERNEL_IDLE_S", "1800"))
_LIMIT = int(os.environ.pop("INDUS_KERNEL_CAPTURE", "1000000"))
os.set_inheritable(_IN, False)
os.set_inheritable(_OUT, False)

_requests = queue.Queue()


def _listen():
    try:
        with os.fdopen(_IN, "rb") as source:
            for line in source:
                _requests.put(line)
    finally:
        os._exit(0)


threading.Thread(target=_listen, name="indus-kernel-requests", daemon=True).start()
_reply = os.fdopen(_OUT, "wb")
_namespace = {"__name__": "__main__", "__builtins__": __builtins__}


def _clip(text):
    if len(text) <= _LIMIT:
        return text, False
    return text[:_LIMIT], True


def _run(number, code):
    name = "<cell %d>" % number
    linecache.cache[name] = (len(code), None, code.splitlines(True), name)
    stdout, stderr = io.StringIO(), io.StringIO()
    state, trace = "ok", ""
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compile(code, name, "exec"), _namespace)
    except SystemExit as exc:
        state, trace = "exit", "SystemExit: %r" % (exc.code,)
    except BaseException:
        state, trace = "error", traceback.format_exc()
    out_text, out_cut = _clip(stdout.getvalue())
    err_text, err_cut = _clip(stderr.getvalue())
    return {
        "state": state,
        "stdout": out_text,
        "stderr": err_text,
        "stdout_cut": out_cut,
        "stderr_cut": err_cut,
        "traceback": trace,
        "count": number,
    }


def main():
    number = 0
    while True:
        try:
            line = _requests.get(timeout=_IDLE_S)
        except queue.Empty:
            os._exit(0)
        try:
            request = json.loads(line)
            code = request["code"]
        except (ValueError, KeyError, TypeError):
            continue
        number += 1
        reply = _run(number, code)
        reply["id"] = request.get("id", "")
        _reply.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\\n")
        _reply.flush()
        if reply["state"] == "exit":
            sys.stdout.flush()
            os._exit(0)


main()
'''

__all__ = ["SOURCE"]
