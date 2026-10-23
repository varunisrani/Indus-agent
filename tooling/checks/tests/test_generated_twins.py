"""Generated twins check: wired into the guard runner, fails on a stale twin (W48-g, W50-f)."""

from __future__ import annotations

import shutil
from pathlib import Path

from tooling.checks import NAMES, generated_twins
from tooling.checks.common import DEFAULT_ROOT, Context

PRICING_JSON = Path("src/indus/providers/pricing_table.json")
FIELD_DOCS_JSON = Path("src/indus/protocol/field_docs.json")
DEFAULTS_JSON = Path("src/indus/foundation/config/defaults.json")
SKILLS_JSON = Path("src/indus/learning/skills/shipped_frontmatter.json")


def _copy_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(DEFAULT_ROOT / "src", root / "src", ignore=ignore)
    # The shipped-skills generator reads the shipped tree beside src/ (W50-f-2).
    shutil.copytree(DEFAULT_ROOT / "skills", root / "skills", ignore=ignore)
    return root


def test_registered() -> None:
    assert "generated_twins" in NAMES


def test_repository_twins_are_current() -> None:
    result = generated_twins.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations
    assert len(result.notes) == 4


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


def test_stale_config_defaults_twin_fails(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    schema_py = root / "src/indus/foundation/config/schema.py"
    schema_py.write_text(schema_py.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    result = generated_twins.run(Context(root=root))
    assert result.code == 1
    assert len(result.violations) == 1
    assert str(DEFAULTS_JSON) in result.violations[0]


def test_stale_shipped_skills_twin_fails(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    skill = next((root / "skills").rglob("SKILL.md"))
    skill.write_text(
        skill.read_text(encoding="utf-8").replace("---\n", "---\nextra_key: 1\n", 1),
        encoding="utf-8",
    )
    result = generated_twins.run(Context(root=root))
    assert result.code == 1
    assert len(result.violations) == 1
    assert str(SKILLS_JSON) in result.violations[0]
