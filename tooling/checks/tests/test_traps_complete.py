"""P25-11: the traps ledger is complete — every research R-id has a row that resolves, and Review
rows name an owner task and a reason (``--strict``: only lead-accepted reviews pass)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tooling.checks import traps
from tooling.checks.common import DEFAULT_ROOT, Context

LEDGER = traps.LEDGER


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def run(root: Path, *, strict: bool = False) -> traps.Result:
    return traps.run(Context(root=root, strict=strict))


@pytest.fixture
def repo(copy_fixture) -> Path:
    """The ledger fixture with its one Review row owned by a task and given a reason, and its
    parametrized reference (a parameter the synthetic test does not have) made plain."""
    root = copy_fixture("ledger_repo")
    edit(root / LEDGER, "test_back_to_front[layers]", "test_back_to_front")
    edit(
        root / LEDGER,
        "Review: covered by manual layout review (P01-widgets)",
        "Review: layout is checked by hand until the paint harness exists (P01-04)",
    )
    return root


def test_real_ledger_passes_with_every_research_id() -> None:
    result = run(DEFAULT_ROOT)
    assert result.code == 0, result.violations[:5]
    assert result.data["research_ids"] == 261
    assert result.data["rows"] >= 261


def test_removing_one_row_fails_with_its_id(repo: Path) -> None:
    assert run(repo).code == 0
    edit(
        repo / LEDGER,
        "- **R01.1** Spinning gears stall\n  - `tests/test_widgets.py::test_spin_yields`\n",
        "",
    )
    result = run(repo)
    assert result.code == 1
    assert any(v.startswith("R01.1 ") and "has no row" in v for v in result.violations)


def test_an_unresolved_test_path_fails(repo: Path) -> None:
    edit(repo / LEDGER, "tests/test_widgets.py::test_drain_once", "tests/test_gone.py::test_x")
    found = "\n".join(run(repo).violations)
    assert "T01.1" in found and "tests/test_gone.py does not exist" in found


@pytest.mark.parametrize(
    "line",
    [
        "Review: checked by hand",  # no owner at all
        "Review: checked by hand (someday)",  # an owner that is not a task id
    ],
)
def test_a_review_without_an_owner_task_fails(repo: Path, line: str) -> None:
    old = "Review: layout is checked by hand until the paint harness exists (P01-04)"
    edit(repo / LEDGER, old, line)
    found = "\n".join(run(repo).violations)
    assert "R01.3: Review line names no owner task" in found


def test_the_placeholder_warns_and_fails_strict(repo: Path) -> None:
    edit(
        repo / LEDGER,
        "Review: layout is checked by hand until the paint harness exists (P01-04)",
        "Review: not yet planned (P00-guards)",
    )
    loose = run(repo)
    assert loose.code == 0
    assert any("R01.3: Review placeholder" in w for w in loose.warnings)
    assert "R01.3: Review placeholder" in "\n".join(run(repo, strict=True).violations)


@pytest.mark.slow
def test_strict_accepts_only_lead_accepted_reviews(repo: Path) -> None:
    found = "\n".join(run(repo, strict=True).violations)
    assert "R01.3: Review row (strict mode)" in found
    edit(
        repo / LEDGER,
        "Review: layout is checked by hand",
        "Review: accepted by the lead 2026-10-21: layout is checked by hand",
    )
    result = run(repo, strict=True)
    assert result.code == 0, result.violations


def test_an_accepted_review_needs_a_reason(repo: Path) -> None:
    edit(
        repo / LEDGER,
        "Review: layout is checked by hand until the paint harness exists (P01-04)",
        "Review: accepted by the lead 2026-10-21: (P01-04)",
    )
    assert "accepted review gives no reason" in "\n".join(run(repo).violations)


@pytest.mark.slow
def test_strict_fails_a_pinning_test_skipped_here(repo: Path) -> None:
    edit(
        repo / LEDGER,
        "Review: layout is checked by hand",
        "Review: accepted by the lead 2026-10-21: layout is checked by hand",
    )
    edit(
        repo / "tests" / "test_widgets.py",
        "def test_spin_yields():",
        "import pytest\n\n\n@pytest.mark.skipif(True, reason='not here')\ndef test_spin_yields():",
    )
    assert run(repo).code == 0  # the file scan treats skipif as a test
    found = "\n".join(run(repo, strict=True).violations)
    assert "R01.1" in found and "is skipped here (not here)" in found


def test_rows_without_a_test_are_listed_as_unpinned(repo: Path) -> None:
    result = run(repo)
    assert result.data["unpinned"] == ["R01.3"]
    assert any(n == "unpinned: R01.3" for n in result.notes)
