"""P25-10: ``acceptance --strict`` — the v1.0 gate over the acceptance map.

Strict mode fails on Review rows, on cited tests that ``pytest --collect-only`` does not collect or
would skip here (and, for TypeScript, on titles the spec scan does not find or finds skipped), on
ACC ids whose feature left the inventory, and on malformed Known gaps rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tooling.checks import acceptance, collect
from tooling.checks.common import Context

pytestmark = pytest.mark.slow  # every strict run starts a pytest collection subprocess

WIDGETS = Path("tests/test_widgets.py")
DECISIONS = """# 16 (synthetic fixture)

### 2.7 Intended differences

| ID | Area |
|---|---|
| D-16.7 | something we changed |

### 2.8 Not in 1.0

| ID | Feature | Indus 1.0 | Why not in 1.0 |
|---|---|---|---|
| D-16.41 | Painting | Not built | Later |

## 3. Decisions
"""


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def run(root: Path, *, strict: bool = True) -> acceptance.Result:
    return acceptance.run(Context(root=root, strict=strict))


def violations(root: Path, *, strict: bool = True) -> str:
    return "\n".join(run(root, strict=strict).violations)


@pytest.fixture
def repo(copy_fixture) -> Path:
    """The ledger fixture with its one Review row resolved: passes ``--strict``."""
    root = copy_fixture("ledger_repo")
    edit(
        root / acceptance.MAP,
        "  - Review: screenshot comparison not automated yet (P01-widgets)\n",
        "  - `tests/test_widgets.py::TestPaint::test_back_to_front`\n",
    )
    (root / acceptance.DECISIONS).write_text(DECISIONS, encoding="utf-8")
    return root


def test_resolved_fixture_passes_strict(repo: Path) -> None:
    result = run(repo)
    assert result.code == 0, result.violations
    assert result.data["review_rows"] == 0


def test_seeded_pending_row_fails_strict_only(repo: Path) -> None:
    append(repo / acceptance.MAP, "- **ACC-A1-2** Spins fast\n  - Review: later (P99-01)\n")
    assert run(repo, strict=False).code == 0
    assert "ACC-A1-2: Review row (strict mode)" in violations(repo)


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        (
            "def test_spin_yields():",
            "import pytest\n\n\n@pytest.mark.skipif(True, reason='not here')\n"
            "def test_spin_yields():",
            "not here",
        ),
        (
            "def test_spin_yields():",
            "import sys\nimport pytest\n\n\n"
            "@pytest.mark.skipif(sys.platform != 'no-such-os', reason='needs no-such-os')\n"
            "def test_spin_yields():",
            "needs no-such-os",
        ),
        (
            '"""\n',
            '"""\nimport pytest\n\npytest.importorskip("no_such_module_for_indus_checks")\n',
            "no_such_module_for_indus_checks",
        ),
        (
            '"""\n',
            '"""\nimport pytest\n\npytest.skip("not today", allow_module_level=True)\n',
            "not today",
        ),
    ],
)
def test_a_test_skipped_here_fails_strict(repo: Path, old: str, new: str, reason: str) -> None:
    edit(repo / WIDGETS, old, new)
    assert run(repo, strict=False).code == 0  # the file scan cannot see these skips
    found = violations(repo)
    assert "ACC-A1-1" in found and "is skipped here" in found and reason in found


def test_a_skipif_that_is_false_here_passes(repo: Path) -> None:
    edit(
        repo / WIDGETS,
        "def test_spin_yields():",
        "import pytest\n\n\n@pytest.mark.skipif(1 > 2, reason='never')\ndef test_spin_yields():",
    )
    assert run(repo).code == 0


def test_a_test_pytest_does_not_collect_fails(repo: Path) -> None:
    helper = repo / "tests" / "widget_helpers.py"  # not a test_*.py file: never collected
    helper.write_text("def test_looks_like_a_test():\n    assert True\n", encoding="utf-8")
    append(
        repo / acceptance.MAP,
        "- **ACC-A1-2** Helper\n  - `tests/widget_helpers.py::test_looks_like_a_test`\n",
    )
    assert run(repo, strict=False).code == 0
    assert "is not collected by pytest" in violations(repo)


def test_a_module_that_fails_to_import_is_a_collection_error(repo: Path) -> None:
    edit(repo / WIDGETS, '"""\n', '"""\nimport no_such_module_for_indus_checks  # noqa: F401\n')
    found = violations(repo)
    assert "collection error" in found and "no_such_module_for_indus_checks" in found


def test_parametrized_references(repo: Path) -> None:
    edit(
        repo / WIDGETS,
        "def test_spin_yields():",
        "import pytest\n\n\n@pytest.mark.parametrize('speed', ['slow', 'fast'])\n"
        "def test_spin_yields(speed):",
    )
    assert run(repo).code == 0  # the bare name matches its parametrized items
    append(
        repo / acceptance.MAP,
        "- **ACC-A1-2** Fast\n  - `tests/test_widgets.py::test_spin_yields[fast]`\n"
        "- **ACC-A1-3** Warp\n  - `tests/test_widgets.py::test_spin_yields[warp]`\n",
    )
    found = violations(repo)
    assert "ACC-A1-2" not in found
    assert "ACC-A1-3" in found and "is not collected" in found


def test_renamed_feature_id_fails_strict_and_warns_otherwise(repo: Path) -> None:
    append(
        repo / acceptance.MAP, "- **ACC-Z7-1** Gone\n  - `tests/test_widgets.py::test_drain_once`\n"
    )
    loose = run(repo, strict=False)
    assert loose.code == 0
    assert any("ACC-Z7-1 names a feature not in" in w for w in loose.warnings)
    assert "ACC-Z7-1 names a feature not in" in violations(repo)


# --------------------------------------------------------------------------- Known gaps


def drop_a2(root: Path) -> None:
    edit(
        root / acceptance.MAP,
        "- **ACC-A2-1** A widget paints\n"
        "  - `tests/test_widgets.py::TestPaint::test_back_to_front`\n",
        "",
    )


def test_a_known_gap_with_a_section_2_8_id_maps_the_feature(repo: Path) -> None:
    drop_a2(repo)
    assert "A2 (P1" in violations(repo)
    append(repo / acceptance.MAP, "\n## Known gaps\n\n- **A2** painting is not built (D-16.41)\n")
    result = run(repo)
    assert result.code == 0, result.violations
    assert result.data["known_gaps"] == ["A2"]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("- **Q9** not a feature (D-16.41)", "Known gap Q9 is not a feature"),
        ("- **A2** painting is not built", "Known gap A2 cites no D-16 id"),
        ("- **A2** painting is not built (D-16.7)", "cites D-16.7, not in §2.8"),
        ("- **A2** painting is not built (D-16.99)", "cites D-16.99, not in §2.8"),
    ],
)
def test_malformed_known_gaps_fail_in_both_modes(repo: Path, row: str, message: str) -> None:
    drop_a2(repo)
    append(repo / acceptance.MAP, f"\n## Known gaps\n\n{row}\n")
    for strict in (False, True):
        found = violations(repo, strict=strict)
        assert message in found
        assert "A2 (P1" in found  # an invalid gap row does not count as mapped


def test_rows_after_known_gaps_are_not_acc_rows(repo: Path) -> None:
    append(repo / acceptance.MAP, "\n## Known gaps\n\n- **A2** extra effects (D-16.41)\n")
    assert run(repo).code == 0


# --------------------------------------------------------------------------- TypeScript

SPEC = """import { describe, expect, it, test } from "vitest";

describe("widgets", () => {
  it("spins", () => {
    expect(1).toBe(1);
  });
  it.skip("paints", () => {});
  it.todo("glows");
  xit("hums", () => {});
  test.skipIf(process.env.CI === "1")("blinks", () => {});
  it(`counts ${"to"} three${process.env.X ? ` (${process.env.X})` : ""}`, () => {});
  // it("commented out", () => {});
  const label = "it('inside a string', () => {})";
});

describe.skip("exports", () => {
  it("writes a file", () => {});
});

describe("nested", () => {
  describe("deeper", () => {
    test("works", async () => {});
  });
});
"""


@pytest.mark.parametrize(
    ("ref", "problem"),
    [
        ("web/widgets.test.ts::spins", None),
        ("web/widgets.test.ts > widgets > spins", None),
        ("web/widgets.test.ts > nested > deeper > works", None),
        ("web/widgets.test.ts::works", None),
        ("web/widgets.test.ts::blinks", None),  # conditional skip: runs where its lane runs
        ("web/widgets.test.ts::counts to three", None),
        ("web/widgets.test.ts::paints", "is skipped"),
        ("web/widgets.test.ts::glows", "is skipped"),
        ("web/widgets.test.ts::hums", "is skipped"),
        ("web/widgets.test.ts > exports > writes a file", "is skipped"),
        ("web/widgets.test.ts > exports > writes", "no describe/it/test"),
        ("web/widgets.test.ts > nested > spins", "no describe/it/test"),
        ("web/widgets.test.ts::commented out", "no describe/it/test"),
        ("web/widgets.test.ts::inside a string", "no describe/it/test"),
    ],
)
def test_typescript_references(repo: Path, ref: str, problem: str | None) -> None:
    spec = repo / "web" / "widgets.test.ts"
    spec.parent.mkdir()
    spec.write_text(SPEC, encoding="utf-8")
    why = collect.ts_status(repo, ref, {})
    if problem is None:
        assert why is None
    else:
        assert why is not None and problem in why


def test_a_skipped_typescript_reference_fails_the_map(repo: Path) -> None:
    spec = repo / "web" / "widgets.test.ts"
    spec.parent.mkdir()
    spec.write_text(SPEC, encoding="utf-8")
    append(
        repo / acceptance.MAP,
        "- **ACC-A1-2** Web spins\n  - `web/widgets.test.ts > widgets > spins`\n"
        "- **ACC-A1-3** Web paints\n  - `web/widgets.test.ts::paints`\n",
    )
    found = violations(repo)
    assert "ACC-A1-2" not in found
    assert "ACC-A1-3" in found and "is skipped" in found


def test_mask_keeps_positions() -> None:
    text = 'a("x") // it("y")\n/* z */ `t${"u"}v`'
    masked = collect.mask(text)
    assert len(masked) == len(text)
    assert masked.count("\n") == 1
    assert "it(" not in masked and '"y"' not in masked
