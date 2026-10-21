"""P25-18: ``similarity --closest N`` ranks regions; ``--report`` writes verdicts and gates.

The synthetic pair under ``fixtures/simrepo`` (invented text): ``ours/src/long_copy.py`` shares
five meaningful lines with ``ref/lib/orchard.py``, ``two_runs.py`` shares two runs a few lines
apart (one region) and one far away (a second region), ``short_copy.py`` shares three lines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tooling.checks import similarity
from tooling.checks.common import Context

REPORT = similarity.REPORT


@pytest.fixture
def pair(copy_fixture, tiny_cache) -> tuple[Path, Path]:
    base = copy_fixture("simrepo")
    return base / "ours", base / "ref"


def run(pair: tuple[Path, Path], n: int = 20, *, report: bool = False) -> similarity.Result:
    ours, ref = pair
    return similarity.run(Context(root=ours, ref=ref, closest=n, report=report))


def set_verdicts(path: Path, verdicts: dict[int, tuple[str, str]]) -> None:
    """Fill the Verdict and Note cells of the numbered rows of the first table."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = similarity._cells(line)  # pyright: ignore[reportPrivateUsage]
        if len(cells) == 9 and cells[0].isdigit() and int(cells[0]) in verdicts:
            verdict, note = verdicts[int(cells[0])]
            line = line.rsplit("|", 3)[0] + f"| {verdict} | {note} |"
        out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def test_regions_are_ranked_by_score_with_line_ranges(pair) -> None:
    result = run(pair)
    assert result.code == 0  # ranking alone never gates
    regions = result.data["regions"]
    assert [r["ours"].split(":")[0] for r in regions] == [
        "src/long_copy.py",
        "src/two_runs.py",
        "src/two_runs.py",
        "src/short_copy.py",
    ]
    assert [r["score"] for r in regions] == sorted((r["score"] for r in regions), reverse=True)
    assert regions[0]["ours"] == "src/long_copy.py:4-13"
    assert regions[0]["ref"] == "lib/orchard.py:4-13"
    assert any(
        n.startswith("  1. src/long_copy.py:4-13 ~ lib/orchard.py:4-13") for n in result.notes
    )


def test_nearby_runs_merge_and_distant_runs_do_not(pair) -> None:
    regions = run(pair).data["regions"]
    two = [r for r in regions if r["ours"].startswith("src/two_runs.py")]
    assert len(two) == 2
    near, far = two
    assert near["ours"] == "src/two_runs.py:4-14" and near["runs"] >= 2
    assert far["ours"].startswith("src/two_runs.py:33")


def test_top_n_limits_the_list(pair) -> None:
    assert len(run(pair, 2).data["regions"]) == 2


def test_closest_without_report_writes_nothing(pair) -> None:
    ours, _ = pair
    run(pair)
    assert not (ours / REPORT).exists()


def test_report_has_one_row_per_region_and_fails_without_verdicts(pair) -> None:
    ours, _ = pair
    result = run(pair, report=True)
    text = (ours / REPORT).read_text(encoding="utf-8")
    rows = [c for c in map(similarity._cells, text.splitlines()) if len(c) == 9]  # pyright: ignore[reportPrivateUsage]
    numbered = [c for c in rows if c[0].isdigit()]
    assert [c[0] for c in numbered] == ["1", "2", "3", "4"]
    assert numbered[0][2] == "src/long_copy.py:4-13" and numbered[0][3] == "lib/orchard.py:4-13"
    assert all(c[7] == "" and c[8] == "" for c in numbered)
    assert result.code == 1
    assert len(result.violations) == 4
    assert all("no verdict yet" in v for v in result.violations)


def test_verdicts_survive_regeneration_and_pass_the_gate(pair) -> None:
    ours, _ = pair
    run(pair, report=True)
    set_verdicts(
        ours / REPORT,
        {
            1: ("independent", "grading loop written from the plan"),
            2: ("convention", "a plain greedy stack"),
            3: ("independent", "counting loop"),
            4: ("protocol-mandated", "the cider record shape is fixed"),
        },
    )
    again = run(pair, report=True)
    assert again.code == 0, again.violations
    text = (ours / REPORT).read_text(encoding="utf-8")
    assert "| independent | grading loop written from the plan |" in text
    assert "| protocol-mandated | the cider record shape is fixed |" in text


@pytest.mark.parametrize(
    ("verdict", "message"),
    [("fix", "marked fix"), ("maybe", "unknown verdict 'maybe'")],
)
def test_fix_or_unknown_verdicts_fail(pair, verdict: str, message: str) -> None:
    ours, _ = pair
    run(pair, report=True)
    filled = {i: ("independent", "ok") for i in range(1, 5)}
    filled[2] = (verdict, "")
    set_verdicts(ours / REPORT, filled)
    result = run(pair, report=True)
    assert result.code == 1
    assert len(result.violations) == 1
    assert "row 2 (src/two_runs.py:4-14)" in result.violations[0]
    assert message in result.violations[0]


def test_a_region_leaving_the_top_list_keeps_its_verdict(pair) -> None:
    ours, _ = pair
    run(pair, report=True)
    set_verdicts(ours / REPORT, {i: ("independent", f"row {i}") for i in range(1, 5)})
    smaller = run(pair, 2, report=True)
    assert smaller.code == 0, smaller.violations
    text = (ours / REPORT).read_text(encoding="utf-8")
    assert "## Reviewed earlier, not in the current top list" in text
    assert "| independent | row 4 |" in text
    assert run(pair, report=True).code == 0  # back in the list with its verdict
