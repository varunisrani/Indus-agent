"""P25-09: step-aligned comparison of two captures, and the classification of what differs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.parity import compare
from scripts.parity.compare import Capture, Divergence, Rules, Step

CAPTURES = Path(__file__).resolve().parents[2] / "fixtures" / "parity" / "captures"


def load(name: str, side: str) -> Capture:
    return compare.load_capture(json.loads((CAPTURES / name).read_text()), side)


@pytest.fixture
def pair() -> tuple[Capture, Capture]:
    return (
        load("synthetic-flow.indus.capture.json", "indus"),
        load("synthetic-flow.ref.capture.json", "reference"),
    )


def kinds(divergences: list[Divergence]) -> list[tuple[str, str | None, str]]:
    return [(d.kind, d.step, d.subject) for d in divergences]


def test_reas_result_shape_and_ours_load_alike(pair: tuple[Capture, Capture]) -> None:
    ours, theirs = pair
    assert [s.step_id for s in ours.steps] == [
        "wait-shell",
        "open-row",
        "wait-detail",
        "indus-only",
    ]
    assert [s.step_id for s in theirs.steps] == [
        "wait-shell",
        "open-row",
        "wait-detail",
        "ref-only",
    ]
    assert theirs.origin == ours.origin == "http://127.0.0.1:24000"
    assert theirs.page_errors == ["boom"] and ours.page_errors == []
    assert ours.steps[1].roles is None  # a click step keeps no roles


def test_divergences_of_the_synthetic_pair(pair: tuple[Capture, Capture]) -> None:
    found = kinds(compare.diverge(*pair))
    assert found == [
        ("step-only-in-indus", "indus-only", "indus-only"),
        ("step-only-in-reference", "ref-only", "ref-only"),
        ("role-only-in-indus", "wait-detail", "list"),
        ("role-only-in-indus", "wait-detail", "listitem"),
        ("role-only-in-reference", "wait-detail", "dialog"),
        ("step-failed-in-reference", "ref-only", "ref-only"),
        ("page-errors", None, "reference"),
    ]


def test_roles_are_compared_only_at_wait_for_steps(pair: tuple[Capture, Capture]) -> None:
    found = compare.diverge(*pair)
    # open-row's accessibility differs a lot between the sides, but it is a click: nothing
    assert all(d.step != "open-row" for d in found)


def test_accessible_names_are_ignored() -> None:
    a = compare.aria_roles('- main:\n  - button "Send it now"\n  - link "Docs"')
    b = compare.aria_roles('- main:\n  - button "Go"\n  - \'link "Read the manual"\'')
    assert a == b == frozenset({"main", "button", "link"})
    assert compare.aria_roles("- text: hello\n- /url: /x") == frozenset()


def test_duplicate_ids_are_an_alignment_failure(pair: tuple[Capture, Capture]) -> None:
    ours = load("duplicate-ids.indus.capture.json", "indus")
    result = compare.compare_flow("synthetic-flow", ours, pair[1], Rules([], []))
    assert result["aligned"] is False and result["divergences"] == []
    assert result["alignment_failures"] == ["duplicate step id 'wait-shell' in the indus capture"]
    assert compare.unclassified(result) == 1


def test_different_origins_are_an_alignment_failure(pair: tuple[Capture, Capture]) -> None:
    ours, theirs = pair
    theirs.origin = "http://127.0.0.1:9999"
    assert compare.alignment_failures(ours, theirs) == [
        "origins differ: http://127.0.0.1:24000 vs http://127.0.0.1:9999"
    ]


def test_classification_intended_gap_unclassified(pair: tuple[Capture, Capture]) -> None:
    rules = Rules(
        intended=[
            {
                "scenario": "synthetic-flow",
                "kind": "role-only-in-reference",
                "subject": "dialog",
                "cite": "D-16.10",
                "why": "ours expands in place",
            },
            {"scenario": "*", "kind": "page-errors", "subject": "*", "cite": "16 §2.5", "why": "x"},
        ],
        gaps=[{"scenario": "synthetic-flow", "steps": ["indus-only"], "why": "no such control"}],
    )
    result = compare.compare_flow("synthetic-flow", *pair, rules)
    classes = {(d["kind"], d["subject"]): d["class"] for d in result["divergences"]}
    assert classes[("role-only-in-reference", "dialog")] == "intended"
    assert classes[("page-errors", "reference")] == "intended"
    assert classes[("step-only-in-indus", "indus-only")] == "reference-gap"
    assert classes[("role-only-in-indus", "list")] == "unclassified"
    assert result["counts"] == {"intended": 2, "reference-gap": 1, "unclassified": 4}
    assert compare.unclassified(result) == 4


def test_compare_all_writes_both_files_and_counts(tmp_path: Path) -> None:
    product = tmp_path / "indus"
    ref = tmp_path / "ref"
    product.mkdir()
    ref.mkdir()
    (product / "synthetic-flow.capture.json").write_text(
        (CAPTURES / "synthetic-flow.indus.capture.json").read_text()
    )
    (ref / "synthetic-flow.ref.capture.json").write_text(
        (CAPTURES / "synthetic-flow.ref.capture.json").read_text()
    )
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"intended": [], "gaps": []}))
    out = tmp_path / "out"
    left = compare.compare_all(product, ref, out, ["synthetic-flow", "absent-flow"], rules)
    assert left == 7
    summary: dict[str, Any] = json.loads((out / "synthetic-flow.compare.json").read_text())
    assert summary["aligned"] is True and summary["counts"] == {"unclassified": 7}
    assert len(json.loads((out / "synthetic-flow.divergences.json").read_text())) == 7
    assert not (out / "absent-flow.compare.json").exists()


def test_terminal_captures_compare_cards_and_prompts() -> None:
    ours = Capture(
        "t",
        "indus",
        [Step("wait-card", "wait_for", "completed", frozenset({"card:approval", "prompt"}))],
    )
    data = {
        "format": "indus-parity-capture/1",
        "flow": "t",
        "steps": [
            {
                "step_id": "wait-card",
                "action": "wait_for",
                "status": "completed",
                "observed": {"cards": [], "prompts": [{"row": 1, "text": ">"}]},
            }
        ],
    }
    theirs = compare.load_capture(data, "reference")
    assert kinds(compare.diverge(ours, theirs)) == [
        ("role-only-in-indus", "wait-card", "card:approval")
    ]


def test_main_exits_1_while_unclassified(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"intended": [], "gaps": []}))
    code = compare.main(
        [
            str(CAPTURES / "synthetic-flow.indus.capture.json"),
            str(CAPTURES / "synthetic-flow.ref.capture.json"),
            "--rules",
            str(rules),
        ]
    )
    assert code == 1
    assert '"unclassified"' in capsys.readouterr().out
