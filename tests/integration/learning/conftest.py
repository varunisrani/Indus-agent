"""CLI runs end like a process does (terminal runtime shut down, foreground fence up); each test
here gets a fresh runtime and an open fence afterwards, as under ``tests/unit/cli``."""

from __future__ import annotations

from tests.unit.cli.conftest import fresh_terminal_runtime

__all__ = ["fresh_terminal_runtime"]
