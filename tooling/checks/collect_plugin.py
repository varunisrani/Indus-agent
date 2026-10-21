"""pytest plugin for the strict ledger checks: record what collection found, as JSON.

Loaded by ``tooling.checks.collect`` with ``-p tooling.checks.collect_plugin`` and active only when
``INDUS_CHECKS_COLLECT_OUT`` names the output file. It records every collected item with the reason
it would be skipped here (``skip`` marks, and ``skipif`` marks whose condition is true), every
collector that skipped as a whole (a module calling ``importorskip`` or a module-level
``pytest.skip``), every collection error, and the ``python_files`` patterns (a file named on the
command line is collected even when a directory run would never pick it up).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

OUT_ENV = "INDUS_CHECKS_COLLECT_OUT"


def _skip_reason(item: pytest.Item) -> str | None:
    from _pytest.skipping import evaluate_skip_marks  # the same rules the run applies

    try:
        found = evaluate_skip_marks(item)
    except Exception as exc:  # a broken condition would error at run time: report it
        return f"skip condition fails: {type(exc).__name__}: {exc}"
    if found is None:
        return None
    return found.reason or "skipped"


def _longrepr_text(report: pytest.CollectReport) -> str:
    rep: Any = report.longrepr
    if isinstance(rep, tuple) and len(rep) == 3:
        return str(rep[2])
    text = str(rep)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "collection error"


class Recorder:
    def __init__(self, out: Path, python_files: list[str]) -> None:
        self.out = out
        self.python_files = python_files
        self.items: dict[str, str | None] = {}
        self.skipped: dict[str, str] = {}
        self.errors: dict[str, str] = {}

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.errors[report.nodeid] = _longrepr_text(report)
        elif report.skipped:
            self.skipped[report.nodeid] = _longrepr_text(report)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            self.items[item.nodeid] = _skip_reason(item)

    def pytest_sessionfinish(self) -> None:
        data = {
            "items": self.items,
            "skipped": self.skipped,
            "errors": self.errors,
            "python_files": self.python_files,
        }
        self.out.write_text(json.dumps(data), encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    out = os.environ.get(OUT_ENV)
    if out:
        patterns = [str(p) for p in config.getini("python_files")]
        recorder = Recorder(Path(out), patterns)
        config.pluginmanager.register(recorder, "indus-checks-collect-recorder")
