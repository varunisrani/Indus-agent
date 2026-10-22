"""P25-20: the release checklist lint (``python -m tooling.checks checklist``)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tooling.checks import NAMES, checklist
from tooling.checks.common import DEFAULT_ROOT, Context

PLAN = """# Plan

## 4. After v1.0

- **D-16.41** an importer.
- **D-16.45** a catalogue.

## 5. Something else

- **D-16.99** not in the after list.
"""

GOOD = """# v1.0 release checklist

## Bug-bash triage

**Ids assigned:** RM1–RM2 · RS1–RS2 · RL1–RL1

### Must-fix for 1.0 (1)

| # | Item | Source | Why |
| --- | --- | --- | --- |
| RM1 | a must item | `docs/notes/x.md` | users lose data |

### Should-fix (2)

| # | Item | Source | Why |
| --- | --- | --- | --- |
| RS1 | a should item | HANDOFF | a workaround exists |
| RS2 | another | LEAD-NOTES | cosmetic |

### Later (1)

| # | Item | Source | Why |
| --- | --- | --- | --- |
| RL1 | a later item | D-16.41 | after 1.0 |

### Done in this wave (1)

| # | Item | What closed it | Commit |
| --- | --- | --- | --- |
| RM2 | fixed thing | `src/x.py`, test `tests/test_x.py` | (lead) |

### Withdrawn (0)

| # | Item | Why withdrawn |
| --- | --- | --- |

### Checked and already fixed (not counted)

- something, fixed.

## After v1.0

- **D-16.41** importer (RL1).
- **D-16.45** catalogue.
"""


def write_tree(root: Path, text: str, plan: str | None = PLAN) -> Path:
    path = root / checklist.CHECKLIST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if plan is not None:
        plan_path = root / checklist.PLAN
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan, encoding="utf-8")
    return root


def violations(root: Path) -> list[str]:
    result = checklist.run(Context(root=root))
    return result.violations


def test_the_real_checklist_passes() -> None:
    result = checklist.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations
    assert (DEFAULT_ROOT / checklist.CHECKLIST).is_file()
    rows = result.data["rows"]
    assert rows["must"] > 0 and rows["should"] > 0 and rows["later"] > 0


def test_a_good_checklist_passes(tmp_path: Path) -> None:
    assert violations(write_tree(tmp_path, GOOD)) == []


def test_no_checklist_is_a_note(tmp_path: Path) -> None:
    result = checklist.run(Context(root=tmp_path))
    assert result.code == 0 and result.notes


def test_a_duplicate_id_fails(tmp_path: Path) -> None:
    text = GOOD.replace("| RS2 | another |", "| RS1 | another |")
    found = violations(write_tree(tmp_path, text))
    assert any("RS1 used again" in v for v in found), found
    assert any("RS2 was assigned but is in no table" in v for v in found), found


def test_a_dropped_id_fails(tmp_path: Path) -> None:
    text = GOOD.replace("### Should-fix (2)", "### Should-fix (1)").replace(
        "| RS2 | another | LEAD-NOTES | cosmetic |\n", ""
    )
    found = violations(write_tree(tmp_path, text))
    assert found == [
        f"{checklist.CHECKLIST}:5: RS2 was assigned but is in no table (move it to Withdrawn "
        "with a reason; never drop or reuse an id)"
    ]


def test_a_withdrawn_id_keeps_its_place(tmp_path: Path) -> None:
    text = (
        GOOD.replace("### Should-fix (2)", "### Should-fix (1)")
        .replace("| RS2 | another | LEAD-NOTES | cosmetic |\n", "")
        .replace(
            "### Withdrawn (0)\n\n| # | Item | Why withdrawn |\n| --- | --- | --- |\n",
            "### Withdrawn (1)\n\n| # | Item | Why withdrawn |\n| --- | --- | --- |\n"
            "| RS2 | another | duplicate of RS1 |\n",
        )
    )
    assert violations(write_tree(tmp_path, text)) == []


def test_an_id_above_the_mark_fails(tmp_path: Path) -> None:
    text = GOOD.replace("### Later (1)", "### Later (2)").replace(
        "| RL1 | a later item | D-16.41 | after 1.0 |\n",
        "| RL1 | a later item | D-16.41 | after 1.0 |\n| RL2 | new | note | why |\n",
    )
    found = violations(write_tree(tmp_path, text))
    assert any("RL2 is above the assigned mark RL1" in v for v in found), found


def test_a_heading_count_must_match_its_rows(tmp_path: Path) -> None:
    text = GOOD.replace("### Should-fix (2)", "### Should-fix (3)")
    found = violations(write_tree(tmp_path, text))
    assert any("heading says 3 rows, the table has 2" in v for v in found), found
    text = GOOD.replace("### Should-fix (2)", "### Should-fix")
    found = violations(write_tree(tmp_path, text))
    assert any("has no (count)" in v for v in found), found


@pytest.mark.parametrize(
    ("old", "new", "column"),
    [
        (
            "| a should item | HANDOFF | a workaround exists |",
            "| a should item |  | a w |",
            "Source",
        ),
        (
            "| a should item | HANDOFF | a workaround exists |",
            "| a should item | HANDOFF | |",
            "Why",
        ),
        ("| RS1 | a should item |", "| RS1 |  |", "Item"),
        ("| `src/x.py`, test `tests/test_x.py` | (lead) |", "| `src/x.py` | |", "Commit"),
    ],
)
def test_every_row_has_its_columns(tmp_path: Path, old: str, new: str, column: str) -> None:
    found = violations(write_tree(tmp_path, GOOD.replace(old, new)))
    assert any(f"empty {column}" in v for v in found), found


def test_a_row_without_an_id_fails(tmp_path: Path) -> None:
    text = GOOD.replace("| RS2 | another |", "| S2 | another |")
    found = violations(write_tree(tmp_path, text))
    assert any("row without an RM/RS/RL id" in v for v in found), found


def test_a_must_row_moved_down_needs_the_owners_dated_approval(tmp_path: Path) -> None:
    moved = GOOD.replace("### Must-fix for 1.0 (1)", "### Must-fix for 1.0 (0)").replace(
        "| RM1 | a must item | `docs/notes/x.md` | users lose data |\n", ""
    )
    without = moved.replace("### Should-fix (2)", "### Should-fix (3)").replace(
        "| RS2 | another | LEAD-NOTES | cosmetic |\n",
        "| RS2 | another | LEAD-NOTES | cosmetic |\n| RM1 | a must item | note | lead wants it |\n",
    )
    found = violations(write_tree(tmp_path, without))
    assert any("RM1 moved from must to should" in v for v in found), found
    approved = without.replace(
        "| lead wants it |",
        '| moved from must: "fine for 1.1" (approved by the owner, 2026-10-23) |',
    )
    assert violations(write_tree(tmp_path, approved)) == []


def test_the_after_list_matches_the_plan(tmp_path: Path) -> None:
    text = GOOD.replace("- **D-16.45** catalogue.\n", "")
    found = violations(write_tree(tmp_path, text))
    assert found == [
        f"{checklist.CHECKLIST}: After v1.0 lacks D-16.45 (listed in {checklist.PLAN} §4)"
    ]
    assert checklist.plan_after_ids(PLAN) == ["D-16.41", "D-16.45"]


def test_a_missing_marks_line_or_table_fails(tmp_path: Path) -> None:
    text = GOOD.replace("**Ids assigned:** RM1–RM2 · RS1–RS2 · RL1–RL1\n", "")
    found = violations(write_tree(tmp_path, text))
    assert any("no 'Ids assigned:' line" in v for v in found), found
    text = GOOD.replace("### Later (1)", "### Someday (1)")
    found = violations(write_tree(tmp_path, text))
    assert any("no 'later' table" in v for v in found), found


def test_tables_inside_a_code_fence_are_ignored(tmp_path: Path) -> None:
    text = GOOD + "\n```markdown\n### Should-fix (9)\n| RS7 | x | y | z |\n```\n"
    assert violations(write_tree(tmp_path, text)) == []


def test_registered_and_runs_alone() -> None:
    assert "checklist" in NAMES
    proc = subprocess.run(
        [sys.executable, "-m", "tooling.checks.checklist"],
        cwd=DEFAULT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "checklist    ok" in proc.stdout
