"""P24-02 guard: nothing under ``src/indus/gateway`` reads the process environment.

Under multiplexing the process env belongs to the launch profile; every served profile reads its
own scope (R06.26). This test parses every gateway module and fails on ``os.environ`` /
``os.environb`` / ``os.getenv`` / ``os.putenv`` / ``os.unsetenv`` (any alias of ``os``),
``from os import environ`` and friends, ``getattr(os, "environ")`` and ``EnvView.from_process()``.

The allow-list is never widened without a written reason in ``docs/notes/P24/multiplex-scope.md``.
It holds one launch-only default, and only for ``from_process``: raw ``os`` reads are never allowed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import indus.gateway
from tooling.checks.no_raw_environ import ENV_NAMES, find_hits

GATEWAY = Path(indus.gateway.__file__).parent

FROM_PROCESS_ALLOWED: dict[str, str] = {
    "control_socket.py": "ControlServer(env=None) falls back to the launch process's env for "
    "supervisor detection; the control socket serves only the launch profile and the daemon "
    "passes scope.env",
}


def _os_aliases(tree: ast.AST) -> set[str]:
    aliases = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(n.asname or "os" for n in node.names if n.name == "os")
    return aliases


def _extra_hits(source: str) -> list[tuple[int, str]]:
    """``from_process()`` calls and ``getattr(os, "environ")``-style reads."""
    tree = ast.parse(source)
    aliases = _os_aliases(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "from_process":
            found.append((node.lineno, "from_process()"))
        elif (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in aliases
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in ENV_NAMES
        ):
            found.append((node.lineno, f"getattr(os, {node.args[1].value!r})"))
    return found


def env_reads(tree_root: Path, allowed: dict[str, str] | None = None) -> list[str]:
    """Every process-env read under ``tree_root`` (paths relative to it), allowed ones removed."""
    allow = FROM_PROCESS_ALLOWED if allowed is None else allowed
    hits: list[str] = []
    for path in sorted(tree_root.rglob("*.py")):
        rel = path.relative_to(tree_root).as_posix()
        source = path.read_text(encoding="utf-8")
        for hit in find_hits(source):
            hits.append(f"{rel}:{hit.line}: {hit.what}")
        for line, what in _extra_hits(source):
            if what == "from_process()" and rel in allow:
                continue
            hits.append(f"{rel}:{line}: {what}")
    return hits


def test_the_gateway_never_reads_the_process_env() -> None:
    assert env_reads(GATEWAY) == []


def test_every_allow_list_entry_has_a_reason_and_still_applies() -> None:
    for rel, reason in FROM_PROCESS_ALLOWED.items():
        assert len(reason.split()) >= 8, rel
        source = (GATEWAY / rel).read_text(encoding="utf-8")
        assert any(what == "from_process()" for _, what in _extra_hits(source)), rel


def test_a_seeded_environ_get_in_an_adapter_fails_the_guard(tmp_path: Path) -> None:
    tree = tmp_path / "gateway"
    adapter = tree / "adapters" / "seeded"
    adapter.mkdir(parents=True)
    (adapter / "__init__.py").write_text("")
    (adapter / "adapter.py").write_text(
        'import os\n\nTOKEN = os.environ.get("SEEDED_TOKEN")\n', encoding="utf-8"
    )
    assert env_reads(tree) == ["adapters/seeded/adapter.py:3: os.environ"]


def test_other_spellings_are_caught_too(tmp_path: Path) -> None:
    tree = tmp_path / "gateway"
    tree.mkdir()
    (tree / "a.py").write_text("from os import getenv\n", encoding="utf-8")
    (tree / "b.py").write_text("import os as o\nx = o.getenv('X')\n", encoding="utf-8")
    (tree / "c.py").write_text("import os\nx = getattr(os, 'environ')\n", encoding="utf-8")
    (tree / "d.py").write_text(
        "from indus.foundation.scope import EnvView\nv = EnvView.from_process()\n",
        encoding="utf-8",
    )
    (tree / "control_socket.py").write_text("import os\nx = os.environ\n", encoding="utf-8")
    assert env_reads(tree) == [
        "a.py:1: from os import getenv",
        "b.py:2: o.getenv",
        "c.py:2: getattr(os, 'environ')",
        "control_socket.py:2: os.environ",
        "d.py:2: from_process()",
    ]
