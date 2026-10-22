"""Generated twins check: wired into the guard runner, fails on a stale twin (W48-g)."""

from __future__ import annotations

import shutil
from pathlib import Path

from tooling.checks import NAMES, generated_twins
from tooling.checks.common import DEFAULT_ROOT, Context

PRICING_JSON = Path("src/indus/providers/pricing_table.json")
FIELD_DOCS_JSON = Path("src/indus/protocol/field_docs.json")


def _copy_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(
        DEFAULT_ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    return root


def test_registered() -> None:
    assert "generated_twins" in NAMES


def test_repository_twins_are_current() -> None:
    result = generated_twins.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations
    assert len(result.notes) == 2


def test_root_without_generators_is_skipped(tmp_path: Path) -> None:
    result = generated_twins.run(Context(root=tmp_path))
    assert result.code == 0
    assert all("nothing to check" in note for note in result.notes)


def test_stale_pricing_twin_fails(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    twin = root / PRICING_JSON
    twin.write_text(twin.read_text(encoding="utf-8") + " ", encoding="utf-8")
    result = generated_twins.run(Context(root=root))
    assert result.code == 1
    assert len(result.violations) == 1
    assert str(PRICING_JSON) in result.violations[0]


def test_stale_field_docs_twin_fails(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    (root / FIELD_DOCS_JSON).write_text("{}\n", encoding="utf-8")
    result = generated_twins.run(Context(root=root))
    assert result.code == 1
    assert len(result.violations) == 1
    assert "indus.protocol.field_docs" in result.violations[0]


def test_broken_generator_is_a_setup_problem(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    generator = root / "src/indus/providers/pricing_twin.py"
    generator.write_text("raise RuntimeError('broken on purpose')\n", encoding="utf-8")
    result = generated_twins.run(Context(root=root))
    assert result.code == 2
    assert "broken on purpose" in result.violations[0]
