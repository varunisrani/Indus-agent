"""Traps ledger and acceptance map, on the synthetic ledger_repo fixture."""

from __future__ import annotations

from pathlib import Path

from tooling.checks import acceptance, ledger, traps
from tooling.checks.common import DEFAULT_ROOT, Context


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


# --------------------------------------------------------------------------- traps


def test_trap_ids_only_from_trap_sections(copy_fixture):
    root = copy_fixture("ledger_repo")
    found = traps.plan_traps(root)
    assert sorted(found) == ["R01.1", "R01.2", "R01.3", "T01.1"]
    assert found["R01.1"][1] == "Spinning gears stall"


def test_fixture_ledger_passes(copy_fixture):
    result = traps.run(Context(root=copy_fixture("ledger_repo")))
    assert result.code == 0, result.violations
    assert result.data["review_rows"] == 1


def test_missing_row_fails(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / traps.LEDGER, "- **T01.1** Queue drains twice\n", "")
    result = traps.run(Context(root=root))
    assert result.code == 1
    assert any(v.startswith("T01.1 ") for v in result.violations)


def test_missing_test_function_and_file_fail(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / traps.LEDGER, "::test_spin_yields", "::test_spin_gone")
    edit(root / traps.LEDGER, "tests/test_widgets.py::test_drain_once", "tests/nope.py::test_x")
    result = traps.run(Context(root=root))
    text = "\n".join(result.violations)
    assert "no test_spin_gone" in text and "tests/nope.py does not exist" in text


def test_class_method_must_exist(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / traps.LEDGER, "TestPaint::test_back_to_front", "TestPaint::test_front")
    assert traps.run(Context(root=root)).code == 1


def test_row_without_coverage_and_bad_line(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / traps.LEDGER, "  - Review: covered by manual layout review (P01-widgets)\n", "")
    edit(root / traps.LEDGER, "  - `tests/test_widgets.py::test_drain_once`", "  - maybe later")
    text = "\n".join(traps.run(Context(root=root)).violations)
    assert "R01.3 has no coverage line" in text
    assert "coverage line must be" in text


def test_missing_ledger_then_skeleton(copy_fixture):
    root = copy_fixture("ledger_repo")
    (root / traps.LEDGER).unlink()
    assert traps.run(Context(root=root)).code == 1
    result = traps.run(Context(root=root, write_skeleton=True))
    assert result.code == 0, result.violations
    text = (root / traps.LEDGER).read_text(encoding="utf-8")
    assert text.count("Review: not yet planned (P00-guards)") == 4
    # Running it again adds nothing and keeps the file as it is.
    traps.run(Context(root=root, write_skeleton=True))
    assert (root / traps.LEDGER).read_text(encoding="utf-8") == text


def test_skeleton_extends_existing_ledger(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / traps.LEDGER, "- **T01.1** Queue drains twice\n", "- **T01.1** Queue drains\n")
    plan = root / "docs" / "plan" / "phases" / "p01.md"
    edit(plan, "| **T01.1** | Queue drains twice |", "| **T01.1** | x |\n| **T01.2** | New |")
    traps.run(Context(root=root, write_skeleton=True))
    text = (root / traps.LEDGER).read_text(encoding="utf-8")
    assert "- **T01.2** New" in text and "`tests/test_widgets.py::test_spin_yields`" in text


def test_repository_ledger_matches_plan():
    result = traps.run(Context(root=DEFAULT_ROOT))
    assert result.code == 0, result.violations[:5]


# --------------------------------------------------------------------------- acceptance


def test_acceptance_passes_and_strict_flags_review(copy_fixture):
    root = copy_fixture("ledger_repo")
    assert acceptance.run(Context(root=root)).code == 0
    strict = acceptance.run(Context(root=root, strict=True))
    assert strict.code == 1 and "Review row (strict mode)" in strict.violations[0]


def test_p0_without_acc_fails_p2_does_not(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / acceptance.MAP, "- **ACC-A1-1** A widget spins", "- **ACC-B1-1** Export")
    result = acceptance.run(Context(root=root))
    assert result.code == 1
    assert any(v.startswith("A1 (P0") for v in result.violations)


def test_acceptance_missing_test(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / acceptance.MAP, "::test_spin_yields", "::test_missing")
    assert acceptance.run(Context(root=root)).code == 1


def test_no_inventory_is_a_notice(tmp_path: Path):
    result = acceptance.run(Context(root=tmp_path))
    assert result.code == 0 and "no inventory yet" in result.notes[0]


def test_acceptance_skeleton(copy_fixture):
    root = copy_fixture("ledger_repo")
    (root / acceptance.MAP).unlink()
    assert acceptance.run(Context(root=root)).code == 1
    assert acceptance.run(Context(root=root, write_skeleton=True)).code == 0
    text = (root / acceptance.MAP).read_text(encoding="utf-8")
    ids = [r for r in ledger.parse(text, "m").rows]
    assert ids == ["ACC-A1-1", "ACC-A2-1", "ACC-A3-1", "ACC-B2-1"]
    assert text.count("Review: not yet planned (phase TBD)") == 4
    # A second run adds nothing.
    acceptance.run(Context(root=root, write_skeleton=True))
    assert (root / acceptance.MAP).read_text(encoding="utf-8") == text


def test_inventory_tiers_mixed_dropped_and_count_tables(copy_fixture):
    root = copy_fixture("ledger_repo")
    features = acceptance.inventory(root)
    tiers = {fid: tier for fid, (_, tier, _) in features.items()}
    # Only area-section rows of the matching letter; the legend, count tables and the
    # stray C1 row inside section B are ignored.
    assert tiers == {
        "A1": "P0",
        "A2": "P1",
        "A3": "P0",  # "P0 → P1": the first tier is the gated one
        "A4": "—",  # dropped
        "B1": "P2",
        "B2": "P1",  # "P1 → P2"
        "B3": "P2",  # "P2 → P3"
    }


def test_dropped_rows_are_not_gated_and_not_warned(copy_fixture):
    root = copy_fixture("ledger_repo")
    result = acceptance.run(Context(root=root))
    assert result.code == 0, result.violations
    assert not any("priority" in w for w in result.warnings)
    assert "7 features, 4 at P0/P1, 1 dropped" in " ".join(result.notes)


def test_mixed_tier_feature_needs_an_acc_id(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / acceptance.MAP, "- **ACC-B2-1** Basic export", "- **ACC-B1-1** Export")
    result = acceptance.run(Context(root=root))
    assert any(v.startswith("B2 (P1") for v in result.violations)


def test_row_without_tier_is_warned(copy_fixture):
    root = copy_fixture("ledger_repo")
    edit(root / acceptance.INVENTORY, "| B3 | Fancy export | P2 → P3 |", "| B3 | Fancy | soon |")
    result = acceptance.run(Context(root=root))
    assert any("B3" in w for w in result.warnings)


def test_skeleton_summary_is_shortened():
    long = "word " * 60
    short = acceptance._short(long)
    assert len(short) <= 102 and short.endswith("…")


# --------------------------------------------------------------------------- parser


def test_parser_ignores_code_fences_and_flags_duplicates():
    text = (
        "- **X1** a\n  - Review: r (T)\n```\n- **X2** b\n```\n- **X1** again\n  - Review: r (T)\n"
    )
    parsed = ledger.parse(text, "l.md")
    assert list(parsed.rows) == ["X1"]
    assert "duplicate row X1" in parsed.problems[0]


def test_review_without_task_id_is_a_warning():
    parsed = ledger.parse("- **X1** a\n  - Review: someday\n", "l.md")
    assert parsed.warnings and not parsed.problems
