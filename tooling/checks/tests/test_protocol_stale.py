"""Protocol stale check: wired into the guard runner, fails on stale generated files."""

from __future__ import annotations

import shutil
from pathlib import Path

from tooling.checks import NAMES, protocol_stale
from tooling.checks.common import DEFAULT_ROOT, Context

GENERATED = Path("packages/protocol/src/generated")


def _copy_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(
        DEFAULT_ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(DEFAULT_ROOT / GENERATED, root / GENERATED)
    return root


def test_registered() -> None:
    assert "protocol_stale" in NAMES


def test_repository_output_is_current() -> None:
    result = protocol_stale.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations


def test_root_without_protocol_package_is_skipped(tmp_path: Path) -> None:
    result = protocol_stale.run(Context(root=tmp_path))
    assert result.code == 0
    assert "nothing to check" in result.notes[0]


def test_stale_copy_fails_with_a_diff(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    types = root / GENERATED / "types.ts"
    types.write_text(types.read_text(encoding="utf-8").replace("seq: number;", "seq: string;"))
    result = protocol_stale.run(Context(root=root))
    assert result.code == 1
    assert "+  seq: number;" in result.violations[0]
    assert "-  seq: string;" in result.violations[0]


def test_broken_generator_is_a_setup_problem(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    generator = root / "src/indus/protocol/codegen.py"
    generator.write_text("raise RuntimeError('broken on purpose')\n", encoding="utf-8")
    result = protocol_stale.run(Context(root=root))
    assert result.code == 2
    assert "broken on purpose" in result.violations[0]
