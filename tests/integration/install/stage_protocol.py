"""The stage protocol as the desktop reads it (P20-09, ``apps/desktop/electron/installer/
stageProtocol.ts``), mirrored for the installer tests: the same last-JSON-line rules, the same
stage-name pattern and protocol version. The tests also run the TypeScript parser itself when Node
can load it (``test_install_sh_core.py``)."""

from __future__ import annotations

import json
import re
from typing import Any

PROTOCOL_VERSION = 1
STAGE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def json_lines(stdout: str) -> list[Any]:
    found: list[Any] = []
    for raw in re.split(r"\r?\n", stdout):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return found


def parse_manifest(stdout: str) -> list[dict[str, Any]] | str:
    """The stages of a ``--manifest`` run, or the error the desktop would show."""
    candidates = [v for v in json_lines(stdout) if isinstance(v, dict) and "stages" in v]
    candidates = [v for v in candidates if isinstance(v["stages"], list)]
    if not candidates:
        return "the installer printed no manifest"
    last = candidates[-1]
    if last.get("protocol_version") != PROTOCOL_VERSION:
        return f"the installer speaks protocol {last.get('protocol_version')}, not 1"
    stages: list[dict[str, Any]] = []
    for raw in last["stages"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            return "the manifest names a stage the desktop cannot run"
        if not STAGE_NAME.match(raw["name"]):
            return "the manifest names a stage the desktop cannot run"
        stages.append(
            {
                "name": raw["name"],
                "title": raw["title"] if isinstance(raw.get("title"), str) else raw["name"],
                "category": raw["category"] if isinstance(raw.get("category"), str) else "",
                "needsUserInput": raw.get("needs_user_input") is True,
            }
        )
    return stages or "the manifest lists no stages"


def parse_stage_frame(stdout: str) -> dict[str, Any] | None:
    """The result frame of a stage run: the last JSON line shaped like one, or None."""
    frames = [
        v
        for v in json_lines(stdout)
        if isinstance(v, dict) and isinstance(v.get("ok"), bool) and isinstance(v.get("stage"), str)
    ]
    if not frames:
        return None
    last = frames[-1]
    return {
        "ok": last["ok"],
        "stage": last["stage"],
        "skipped": last.get("skipped") is True,
        "reason": last["reason"] if isinstance(last.get("reason"), str) else None,
    }
