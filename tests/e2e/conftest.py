"""Demo 1 harness: the real ``indus`` entry point in a pseudo-terminal against the fake model.

``Demo.spawn(*args, script=...)`` starts ``python -m tests.integration.cli.fake_runner …`` (the
real entry with the fake model route, P06-12's driver) under ``pexpect`` in a temporary home and
working folder, with a pinned terminal id so ``indus -c`` finds this terminal's breadcrumb. What
the terminal printed is kept in ``Demo.transcript`` (escape sequences stripped) for failure
messages. ``Demo.rows(session_id)`` reads the stored transcript back.

CLI runs end like a process does; tests that also run CLI code in this interpreter get a fresh
terminal runtime and an open foreground fence afterwards (as in ``tests/unit/cli``).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.backends.wait import FOREGROUND
from indus.tools.builtin import terminal

pexpect = pytest.importorskip("pexpect")

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 60
TERMINAL_ID = "e2e-demo1"
_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07|[@-Z\\-_])")


def plain(text: str) -> str:
    return _ESCAPES.sub("", text).replace("\r", "")


def write_script(folder: Path, name: str, turns: list[list[dict[str, Any]]]) -> Path:
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"turns": [{"steps": steps} for steps in turns]}), encoding="utf-8")
    return path


class Demo:
    def __init__(self, base: Path) -> None:
        self.home = base / "home"
        self.work = base / "work"
        self.home.mkdir()
        self.work.mkdir()
        self.log = io.StringIO()

    def env(self, script: Path | str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
        env.update(
            {
                "INDUS_HOME": str(self.home),
                "INDUS_TEST_MODEL_SCRIPT": str(script),
                "INDUS_TERMINAL_ID": TERMINAL_ID,
                "PYTHONPATH": str(ROOT),
                "TERM": "xterm-256color",
                "COLUMNS": "110",
                "LINES": "40",
            }
        )
        return env

    def spawn(self, *args: str, script: Path | str = "demo1") -> Any:
        child = pexpect.spawn(
            sys.executable,
            ["-m", "tests.integration.cli.fake_runner", *args],
            cwd=str(self.work),
            env=self.env(script),
            encoding="utf-8",
            codec_errors="replace",
            dimensions=(40, 110),
            timeout=TIMEOUT,
        )
        child.logfile_read = self.log
        return child

    def run(self, *args: str, script: Path | str = "demo1") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "tests.integration.cli.fake_runner", *args],
            capture_output=True,
            text=True,
            cwd=self.work,
            env=self.env(script),
            timeout=TIMEOUT,
            check=False,
        )

    @property
    def transcript(self) -> str:
        return plain(self.log.getvalue())

    def finish(self, child: Any) -> int | None:
        """Wait for the process to end; its exit status (None when a signal ended it)."""
        child.expect(pexpect.EOF)
        child.close()
        return child.exitstatus

    def session_ids(self) -> list[str]:
        from indus.foundation.scope import make_scope
        from indus.state import listing, registry

        with registry.open_unshared(make_scope(self.home)) as handle:
            found = listing.list_sessions(handle, listing.ListFilters(), limit=50)
        return [item.id for item in found]

    def rows(self, session_id: str) -> list[Any]:
        from indus.core.persist import StateStore
        from indus.foundation.scope import make_scope
        from indus.state import registry

        with registry.open_unshared(make_scope(self.home)) as handle:
            return list(asyncio.run(StateStore(handle).load(session_id)))


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[Demo]:
    base = tmp_path / "demo1"
    base.mkdir()
    made = Demo(base)
    yield made
    from indus.state import registry

    registry.close_all()


@pytest.fixture(autouse=True)
def fresh_terminal_runtime() -> Iterator[None]:
    terminal._shared.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    terminal._shared.clear()  # pyright: ignore[reportPrivateUsage]
    FOREGROUND.reset_for_tests()
