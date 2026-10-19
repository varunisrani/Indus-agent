# pyright: strict
"""The program a remote session kernel runs (P10-11), kept as text and shipped to the backend.

It starts its own session (so Indus can stop it as a group), writes ``ready`` into its kernel
directory, then polls ``cells/`` for ``req_<n>.json``. Each request is removed, run in the one
namespace kept for the kernel's life with file descriptors 1 and 2 pointed at private temp files
(so output of programs the cell starts is caught too, each stream cut to
``INDUS_KERNEL_CAPTURE`` bytes), and answered with ``res_<n>.json`` (temp name, then rename,
mode 0600). It exits when the kernel directory disappears, after ``INDUS_KERNEL_IDLE_S`` seconds
without a request, or after a cell that called ``sys.exit``.
"""

from __future__ import annotations

SOURCE = '''\
"""Indus Agent remote session kernel: runs execute_code cells shipped as files."""
import json
import linecache
import os
import sys
import tempfile
import time
import traceback

try:
    os.setsid()
except OSError:
    pass

_DIR = os.environ["INDUS_KERNEL_DIR"]
_CELLS = os.path.join(_DIR, "cells")
_IDLE_S = float(os.environ.get("INDUS_KERNEL_IDLE_S", "1800"))
_LIMIT = int(os.environ.get("INDUS_KERNEL_CAPTURE", "1000000"))
_namespace = {"__name__": "__main__", "__builtins__": __builtins__}
try:  # keep printed lines in order with output of programs a cell starts
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass


def _grab(handle):
    handle.seek(0)
    data = handle.read(_LIMIT + 1)
    cut = len(data) > _LIMIT
    return data[:_LIMIT].decode("utf-8", errors="replace"), cut


def _run(number, code):
    name = "<cell %d>" % number
    linecache.cache[name] = (len(code), None, code.splitlines(True), name)
    state, trace = "ok", ""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = (os.dup(1), os.dup(2))
    out_file = tempfile.TemporaryFile(dir=_DIR)
    err_file = tempfile.TemporaryFile(dir=_DIR)
    os.dup2(out_file.fileno(), 1)
    os.dup2(err_file.fileno(), 2)
    try:
        exec(compile(code, name, "exec"), _namespace)
    except SystemExit as exc:
        state, trace = "exit", "SystemExit: %r" % (exc.code,)
    except BaseException:
        state, trace = "error", traceback.format_exc()
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])
    out_text, out_cut = _grab(out_file)
    err_text, err_cut = _grab(err_file)
    out_file.close()
    err_file.close()
    return {
        "state": state,
        "stdout": out_text,
        "stderr": err_text,
        "stdout_cut": out_cut,
        "stderr_cut": err_cut,
        "traceback": trace,
        "count": number,
    }


def _answer(name, reply):
    final = os.path.join(_CELLS, name.replace("req_", "res_", 1))
    partial = final + ".part"
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(reply, handle, ensure_ascii=False)
    os.replace(partial, final)


def main():
    open(os.path.join(_DIR, "ready"), "w").close()
    number = 0
    last = time.monotonic()
    while True:
        if not os.path.isdir(_CELLS):
            return
        names = sorted(
            n for n in os.listdir(_CELLS) if n.startswith("req_") and n.endswith(".json")
        )
        if not names:
            if time.monotonic() - last > _IDLE_S:
                return
            time.sleep(0.1)
            continue
        for name in names:
            path = os.path.join(_CELLS, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    request = json.load(handle)
                code = request["code"]
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            os.remove(path)
            last = time.monotonic()
            number += 1
            reply = _run(number, code)
            reply["id"] = request.get("id", "")
            _answer(name, reply)
            if reply["state"] == "exit":
                return


main()
'''

__all__ = ["SOURCE"]
