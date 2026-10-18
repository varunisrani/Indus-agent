"""P06-15: the acceptance map check (D-15.3) — seeded violations fail, the real map passes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tooling.checks import acceptance
from tooling.checks.common import DEFAULT_ROOT, Context

WIDGETS = Path("tests/test_widgets.py")


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def violations(root: Path, *, strict: bool = False) -> str:
    result = acceptance.run(Context(root=root, strict=strict))
    return "\n".join(result.violations)


def test_repository_map_passes_non_strict() -> None:
    result = acceptance.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations[:5]


def test_p0_feature_without_an_id_fails(copy_fixture) -> None:
    root = copy_fixture("ledger_repo")
    edit(root / acceptance.MAP, "- **ACC-A1-1** A widget spins\n", "")
    edit(root / acceptance.MAP, "  - `tests/test_widgets.py::test_spin_yields`\n", "")
    assert "A1 (P0" in violations(root)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("def test_spin_yields():", "@pytest.mark.skip(reason='later')\ndef test_spin_yields():"),
        ("def test_spin_yields():", "@mark.skip\ndef test_spin_yields():"),
        (
            "def test_spin_yields():\n    assert True",
            'def test_spin_yields():\n    """Doc."""\n    pytest.skip("later")',
        ),
        ("class TestPaint:", "@pytest.mark.skip\nclass TestPaint:"),
    ],
)
def test_a_cited_test_that_is_skipped_fails(copy_fixture, old: str, new: str) -> None:
    root = copy_fixture("ledger_repo")
    edit(root / WIDGETS, old, new)
    assert "is skipped" in violations(root)


def test_a_skipped_module_fails(copy_fixture) -> None:
    root = copy_fixture("ledger_repo")
    edit(root / WIDGETS, '"""\n', '"""\nimport pytest\n\npytestmark = [pytest.mark.skip]\n')
    assert "the whole module is skipped" in violations(root)


def test_conditional_skips_still_count(copy_fixture) -> None:
    root = copy_fixture("ledger_repo")
    edit(
        root / WIDGETS,
        "def test_spin_yields():",
        "@pytest.mark.skipif(False, reason='never')\ndef test_spin_yields():",
    )
    assert acceptance.run(Context(root=root)).code == 0


def test_strict_fails_on_review_rows(copy_fixture) -> None:
    root = copy_fixture("ledger_repo")
    assert acceptance.run(Context(root=root)).code == 0
    assert "Review row (strict mode)" in violations(root, strict=True)


def test_row_with_neither_test_nor_review_fails(copy_fixture) -> None:
    root = copy_fixture("ledger_repo")
    edit(
        root / acceptance.MAP,
        "  - Review: screenshot comparison not automated yet (P01-widgets)\n",
        "",
    )
    assert "ACC-A2-1 has no coverage line" in violations(root)
