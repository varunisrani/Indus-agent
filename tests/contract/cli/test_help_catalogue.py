"""P07-26: the help catalogue.

- ``indus --help`` lists the top-level commands in registration order, which is pinned here as a
  golden list (research 04 §3.2.4 order);
- every command and subcommand answers ``--help`` with exit 0 and has a one-line description;
- ``indus NAME --help`` in a fresh process imports NAME's own module and no other top-level
  command's module (lazy loading), apart from the shared ones named in ``SHARED_IMPORTS``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.foundation.scope import Scope
from tests.contract.cli._walk import commands, key, walk

GOLDEN_ORDER = [
    "chat",
    "model",
    "fallback",
    "setup",
    "auth",
    "status",
    "doctor",
    "approvals",
    "dump",
    "backup",
    "checkpoints",
    "worktree",
    "import",
    "config",
    "tools",
    "sessions",
    "profile",
    "completion",
    "logs",
    "bundles",
    "skills",
    "memory",
    "curator",
    "journey",
    "learning",
    "moa",
    "mcp",
    "hooks",
    "plugins",
    "cron",
    "gateway",  # P15-28
    "pairing",  # P15-09
    "send",  # P15-20
    "webhook",  # P15-32
    "kanban",  # P24-13 (research 04: after webhook and peer)
    "project",  # P24-14
    "dashboard",  # P18-01
    "serve",  # P18-01
    "research",
    "uninstall",  # P23-20
    "acp",  # P19-01
    "update",  # P23-19
    "peer",  # P24-07
    "lsp",  # P24-15
    "verify",  # P24-17
    "import-agent",  # P24-18
    "pm",  # P24-24
    "egress",  # P24-42
    "vault",  # P24-40
    "blueprint",  # P24-46
    "pet",  # P24-19
    "console",  # P24-36
]

SHARED_IMPORTS: dict[str, set[str]] = {
    "dashboard": {"serve"},  # one module, two commands (P18-01)
    "serve": {"dashboard"},
    "dump": {"status"},
    "journey": {"learning"},  # one module, two names (P09-23 alias)
    "learning": {"journey"},
    "skills": {"memory"},  # shares the pending-write helpers (P09-24)
}
"""Top-level command → other top-level commands whose module it may import (it reuses them)."""

_PROBE = """
import json, os, sys, contextlib, io
from indus.cli import entry
from indus.cli.app import REGISTRY
name, home = sys.argv[1], sys.argv[2]
env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
env.update({"INDUS_HOME": home, "HOME": home, "USERPROFILE": home})
with contextlib.redirect_stdout(io.StringIO()):
    code = entry.main([name, "--help"], env=env)
modules = {n: t.split(":")[0] for n, t, _ in REGISTRY}
loaded = sorted(n for n, m in modules.items() if m in sys.modules)
print(json.dumps({"code": code, "loaded": loaded}))
"""


def test_top_level_order_is_the_golden_list(scope: Scope) -> None:
    assert [name for name, _, _ in app_mod.REGISTRY] == GOLDEN_ORDER
    result = CliRunner().invoke(app_mod.cli, ["--help"], obj=CliState(scope=scope))
    assert result.exit_code == 0
    section = result.output.split("Commands:", 1)[1]
    listed = [line.split()[0] for line in section.splitlines() if line.strip()]
    assert listed == GOLDEN_ORDER


@pytest.mark.parametrize("name", [key(path) for path in commands()])
def test_every_command_has_help(scope: Scope, name: str) -> None:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(app_mod.cli, [*name.split(), "--help"], obj=state)
    assert result.exit_code == 0, result.output
    assert f"Usage: indus {name}" in result.output


def test_every_command_has_a_one_line_description() -> None:
    missing = [key(path) for path, command in walk(app_mod.cli) if not command.get_short_help_str()]
    assert missing == []


def _probe(name: str, home: Path) -> dict[str, object]:
    home.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, name, str(home)],
        capture_output=True,
        text=True,
        timeout=120,
        env={k: v for k, v in os.environ.items() if not k.startswith("INDUS_")},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def test_help_imports_only_that_command(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        found = dict(
            zip(
                GOLDEN_ORDER,
                pool.map(lambda n: _probe(n, tmp_path / n), GOLDEN_ORDER),
                strict=True,
            )
        )
    problems: list[str] = []
    for name, result in found.items():
        loaded = {str(n) for n in cast(list[object], result["loaded"])}
        extra = loaded - {name} - SHARED_IMPORTS.get(name, set())
        if result["code"] != 0 or name not in loaded or extra:
            problems.append(f"{name}: exit {result['code']}, imported {sorted(loaded)}")
    assert problems == []
