"""P06-15: the traps ledger check (D-15.3) — seeded violations fail, the real ledger passes."""

from __future__ import annotations

from pathlib import Path

from tooling.checks import traps
from tooling.checks.common import DEFAULT_ROOT, Context


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_repository_ledger_passes() -> None:
    result = traps.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations[:5]
    assert result.data["rows"] >= result.data["traps"]


def test_a_new_r_id_in_a_research_spec_without_a_row_fails(copy_fixture) -> None:
    root = copy_fixture("ledger_repo")
    spec = root / "docs" / "plan" / "research" / "01-widgets.md"
    edit(
        spec,
        "- `R01.3` Backticked",
        "3. **R01.4 Fresh trap.** Not in the ledger.\n- `R01.3` Backticked",
    )
    result = traps.run(Context(root=root))
    assert result.code == 1
    assert any(v.startswith("R01.4 ") for v in result.violations)


def test_a_skipped_pinning_test_fails(copy_fixture) -> None:
    root = copy_fixture("ledger_repo")
    edit(
        root / "tests" / "test_widgets.py",
        "async def test_drain_once():",
        "@pytest.mark.skip\nasync def test_drain_once():",
    )
    result = traps.run(Context(root=root))
    assert result.code == 1
    assert any("test_drain_once is skipped" in v for v in result.violations)
