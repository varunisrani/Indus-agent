"""A scripted language-server client and manager for the edit-check tests: verdicts computed from
the synced text (``BAD(x)`` is an error, ``WARN(x)`` a warning), released when the test says."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from indus.foundation.clock import Clock, SystemClock
from indus.tools.lsp.client import as_synced
from indus.tools.lsp.diagnostics import EditDiagnostics
from indus.tools.lsp.manager import LspSettings
from indus.tools.lsp.servers import ServerSpec

RULE = re.compile(r"(BAD|WARN)\(([^)]*)\)")
SPEC = ServerSpec("fake-ls", (".fk",), ("fake-ls",), project_code=False)


def verdict_for(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
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


class ScriptedClient:
    server_id = "fake-ls"

    def __init__(self) -> None:
        self.docs: dict[str, tuple[int, str]] = {}
        self.gate: asyncio.Event | None = None
        self.silent = False  # never gives a verdict
        self.silent_for: set[str] = set()  # texts it gives no verdict on
        self.hang = False  # never answers at all
        self.active = 0
        self.most_active = 0
        self.asked: list[str] = []

    @property
    def running(self) -> bool:
        return True

    async def sync(self, path: str, text: str | None = None, *, language: str = "") -> int:
        key = os.path.abspath(path)
        if text is None:
            text = Path(key).read_text(encoding="utf-8")
        version = self.docs[key][0] + 1 if key in self.docs else 0
        self.docs[key] = (version, as_synced(text))
        return version

    async def save(self, path: str) -> None:
        return None

    def synced_text(self, path: str) -> str | None:
        found = self.docs.get(os.path.abspath(path))
        return found[1] if found else None

    async def diagnostics(
        self, path: str, version: int, *, timeout: float
    ) -> list[dict[str, Any]] | None:
        key = os.path.abspath(path)
        text = self.docs[key][1]
        self.asked.append(text)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        try:
            if self.hang:
                await asyncio.Event().wait()
            if self.gate is not None:
                await self.gate.wait()
            if self.silent or text in self.silent_for:
                return None
            return verdict_for(text)
        finally:
            self.active -= 1


class ScriptedManager:
    """What ``EditDiagnostics`` asks of a manager, answered by one ``ScriptedClient``."""

    def __init__(self, clock: Clock | None = None, *, wait_timeout: float = 5.0) -> None:
        self.clock: Clock = clock or SystemClock()
        self.settings = LspSettings(wait_timeout=wait_timeout, servers=(SPEC,))
        self.client = ScriptedClient()
        self.dropped: list[tuple[str, str]] = []

    def handles(self, path: str) -> bool:
        return path.endswith(".fk")

    def server_for(self, path: str) -> ServerSpec | None:
        return SPEC if path.endswith(".fk") else None

    async def client_for(self, path: str) -> ScriptedClient | None:
        return self.client if path.endswith(".fk") else None

    def touch_client(self, client: object) -> None:
        return None

    async def drop(self, path: str, reason: str) -> None:
        self.dropped.append((path, reason))


def checker(clock: Clock | None = None, **kw: Any) -> tuple[EditDiagnostics, ScriptedManager]:
    manager = ScriptedManager(clock)
    return EditDiagnostics(manager, **kw), manager  # type: ignore[arg-type]
