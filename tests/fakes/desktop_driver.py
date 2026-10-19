"""A fake desktop driver program for the ``computer_use`` tests (P10-20).

Run as ``python desktop_driver.py --log <file> [options]``. It speaks the Indus stdio protocol:
one JSON request per line on stdin, one JSON answer per line on stdout, and exits when stdin
closes. Every request it receives is appended to the log file as one JSON line, so a test can
prove that an action never reached the driver.

Options:
- ``--not-ready <reason>``: ``hello`` answers ``ready: false`` with that reason;
- ``--permissions ax=1,sr=0``: the permission booleans ``hello`` reports;
- ``--crash-on <op>``: exit with code 3 (after a line on stderr) when that op arrives;
- ``--crash-once <op>``: the same, but only the first time across runs (a marker file next to
  the log remembers it);
- ``--hang-on <op>``: never answer that op;
- ``--refuse <op>``: answer that op with ``ok: false``;
- ``--elements <n>``: how many controls a capture lists (default 3);
- ``--tiny`` / ``--bad-image``: captures carry a 2x2 PNG / bytes that are no image;
- ``--garbage-on <op>``: answer that op with a line that is not JSON.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
from pathlib import Path
from typing import Any

# A 16x16 PNG (large enough for every provider) and a 2x2 one (too small to send).
PNG_16X16 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR42mNQSFhAEmIY1TCqYfhqAACc2CAQXBvA"
    "NQAAAABJRU5ErkJggg=="
)
PNG_2X2 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJ"
    "RU5ErkJggg=="
)


def _permissions(text: str) -> dict[str, bool]:
    names = {"ax": "accessibility", "sr": "screen_recording"}
    out: dict[str, bool] = {}
    for part in text.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            out[names.get(key.strip(), key.strip())] = value.strip() == "1"
    return out


def _capture(args: dict[str, Any], count: int, image: str) -> dict[str, Any]:
    mode = args.get("mode", "som")
    elements = [
        {
            "index": i,
            "role": "button",
            "label": f"Button {i}",
            "bounds": [10 * i, 20, 40, 16],
            "app": args.get("app") or "Notes",
        }
        for i in range(1, count + 1)
    ]
    result: dict[str, Any] = {
        "mode": mode,
        "width": 1280,
        "height": 800,
        "app": args.get("app") or "Notes",
        "window": "Untitled",
        "elements": elements,
    }
    if mode == "ax":
        result["tree"] = "window 'Untitled'\n" + "\n".join(
            f"  button 'Button {e['index']}'" for e in elements
        )
    else:
        result["image"] = image
        result["mime"] = "image/png"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--not-ready", default=None)
    parser.add_argument("--permissions", default="")
    parser.add_argument("--crash-on", default=None)
    parser.add_argument("--crash-once", default=None)
    parser.add_argument("--hang-on", default=None)
    parser.add_argument("--refuse", default=None)
    parser.add_argument("--garbage-on", default=None)
    parser.add_argument("--elements", type=int, default=3)
    parser.add_argument("--tiny", action="store_true")
    parser.add_argument("--bad-image", action="store_true")
    opts = parser.parse_args()
    image = PNG_2X2 if opts.tiny else PNG_16X16
    if opts.bad_image:
        image = base64.b64encode(b"definitely not an image").decode()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        request = json.loads(line)
        op, args, rid = request.get("op"), request.get("args") or {}, request.get("id")
        with open(opts.log, "a", encoding="utf-8") as log:
            log.write(json.dumps({"op": op, "args": args}) + "\n")
        marker = Path(opts.log + ".crashed")
        if op == opts.crash_on or (op == opts.crash_once and not marker.exists()):
            marker.touch()
            print(f"fake driver: crashing on {op}", file=sys.stderr, flush=True)
            return 3
        if op == opts.hang_on:
            threading.Event().wait(3600)
        if op == opts.garbage_on:
            print("this is not json", flush=True)
            continue
        if op == opts.refuse:
            answer: dict[str, Any] = {"id": rid, "ok": False, "error": f"cannot {op} here"}
        elif op == "hello":
            result: dict[str, Any] = {"ready": opts.not_ready is None, "name": "fake-desktop",
                                      "version": "0.1"}  # fmt: skip
            if opts.not_ready is not None:
                result["reason"] = opts.not_ready
            if opts.permissions:
                result["permissions"] = _permissions(opts.permissions)
            answer = {"id": rid, "ok": True, "result": result}
        elif op == "capture":
            answer = {"id": rid, "ok": True, "result": _capture(args, opts.elements, image)}
        elif op == "list_apps":
            apps = [{"name": "Notes", "pid": 101}, {"name": "Terminal", "pid": 102}]
            answer = {"id": rid, "ok": True, "result": {"apps": apps}}
        elif op == "wait":
            threading.Event().wait(min(int(args.get("ms", 0)), 2000) / 1000)
            answer = {"id": rid, "ok": True, "result": {"message": "waited"}}
        else:
            answer = {"id": rid, "ok": True, "result": {"message": f"{op} done", "echo": args}}
        print(json.dumps(answer), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
