"""P25-08: the obligation ledger — its self-test and the statuses read from captures."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.parity import ledger
from scripts.parity.ledger import Ledger


@pytest.fixture
def real() -> Ledger:
    return Ledger.load()


def variant(base: Ledger, **changes: Any) -> Ledger:
    data = {
        "obligations": copy.deepcopy(base.obligations),
        "bindings": copy.deepcopy(base.bindings),
        "intended": copy.deepcopy(base.intended),
        "gaps": copy.deepcopy(base.gaps),
    }
    data.update(changes)
    return Ledger(**data)


def test_real_ledger_self_test_is_green(real: Ledger) -> None:
    assert ledger.self_test(real) == []
    assert len(real.obligations) == len(real.bindings) >= 12


def test_every_scenario_proves_an_obligation(real: Ledger) -> None:
    from scripts.parity.runner import FLOWS

    assert {o["scenario"] for o in real.obligations} == {f.flow for f in FLOWS}
    dropped = variant(
        real, obligations=[o for o in real.obligations if o["scenario"] != "acp-session"]
    )
    problems = ledger.self_test(dropped)
    assert "acp-session: proves no obligation" in problems


def test_a_rule_citing_a_missing_d_id_fails(real: Ledger) -> None:
    rule = {"scenario": "cli-chat-approve", "kind": "page-errors", "subject": "*", "why": "x"}
    good = variant(real, intended=[{**rule, "cite": "D-16.10"}, {**rule, "cite": "16 §2.7"}])
    assert ledger.self_test(good) == []
    bad = variant(real, intended=[{**rule, "cite": "D-16.99999"}, {**rule, "cite": "trust me"}])
    problems = ledger.self_test(bad)
    assert "intended[0]: cites D-16.99999, which has no row in 16-what-we-change.md" in problems
    assert any(p.startswith("intended[1]: cite 'trust me'") for p in problems)
    no_why = variant(real, intended=[{**rule, "cite": "D-16.10", "why": " "}])
    assert "intended[0]: no why" in ledger.self_test(no_why)


def test_a_manifest_pointing_at_a_missing_symbol_fails(real: Ledger) -> None:
    bindings = copy.deepcopy(real.bindings)
    bindings[0]["owner"]["symbol"] = "no_such_symbol"
    bindings[1]["owner"]["module"] = "indus.no_such_module"
    ts = next(b for b in bindings if "/" in b["owner"]["module"])
    ts["owner"]["symbol"] = "NoSuchExport"
    problems = ledger.self_test(variant(real, bindings=bindings))
    assert (
        f"{bindings[0]['obligation']}: indus.cli.setup.wizard does not export no_such_symbol"
        in problems
    )
    second = bindings[1]
    missing = f"indus.no_such_module does not export {second['owner']['symbol']}"
    assert f"{second['obligation']}: {missing}" in problems
    assert any(p.startswith(f"{ts['obligation']}: apps/") and "NoSuchExport" in p for p in problems)


def test_unknown_steps_tests_and_duplicate_bindings_fail(real: Ledger) -> None:
    obligations = copy.deepcopy(real.obligations)
    obligations[0]["steps"].append("no-such-step")
    bindings = copy.deepcopy(real.bindings)
    bindings[0]["tests"] = [
        "tests/unit/nowhere.py::test_x",
        "apps/web/e2e/chat.spec.ts > no such title",
    ]
    bindings.append(copy.deepcopy(bindings[1]))
    problems = ledger.self_test(variant(real, obligations=obligations, bindings=bindings))
    first = obligations[0]["id"]
    assert f"{first}: step 'no-such-step' is not in {obligations[0]['scenario']}" in problems
    assert f"{first}: test 'tests/unit/nowhere.py::test_x' does not resolve" in problems
    assert any("no such title" in p for p in problems)
    assert f"{bindings[1]['obligation']}: 2 manifest bindings (exactly one wanted)" in problems


def test_gaps_need_a_reason_and_real_steps(real: Ledger) -> None:
    gaps = [
        {"scenario": "cli-chat-approve", "steps": ["deny"], "why": ""},
        {"scenario": "cli-chat-approve", "steps": ["no-step"], "why": "x"},
    ]
    problems = ledger.self_test(variant(real, gaps=gaps))
    assert "gaps[0]: a gap needs a reason" in problems
    assert "gaps[1]: step 'no-step' is not in cli-chat-approve" in problems


def test_bad_obligation_ids_and_duplicates(real: Ledger) -> None:
    obligations = copy.deepcopy(real.obligations)
    obligations[0]["id"] = "obl-1"
    obligations.append(copy.deepcopy(obligations[1]))
    problems = ledger.self_test(variant(real, obligations=obligations))
    assert "obl-1: id must look like OBL-<AREA>-<nnn>" in problems
    assert f"{obligations[1]['id']}: listed 2 times" in problems


def test_resolvers() -> None:
    assert ledger.exports_symbol("indus.cli.resume", "resume_session")
    assert not ledger.exports_symbol("indus.cli.resume", "_nothing_here")
    assert ledger.exports_symbol("apps/web/src/pages/SessionsPage.tsx", "SessionsPage")
    assert ledger.test_resolves("tests/unit/parity/test_ledger.py::test_resolvers")
    assert ledger.test_resolves(
        "apps/tui/e2e/demo2.e2e.test.ts > Ctrl+O switches the model for the session; "
        "the next request uses it"
    )
    assert not ledger.test_resolves("tests/unit/parity/test_ledger.py::test_nope")
    assert not ledger.test_resolves("just words")


# -- statuses ------------------------------------------------------------------------------------


def _capture(
    flow: str, done: list[str], failed: tuple[str, ...] = (), status: str = "passed"
) -> dict[str, Any]:
    steps = [{"step_id": s, "action": "wait_for", "status": "completed"} for s in done]
    steps += [{"step_id": s, "action": "wait_for", "status": "failed"} for s in failed]
    return {
        "format": "indus-parity-capture/1",
        "flow": flow,
        "status": status,
        "reason": "",
        "steps": steps,
    }


def _small() -> Ledger:
    obligations = [
        {
            "id": f"OBL-T-00{n}",
            "title": "t",
            "family": "t",
            "scenario": f"flow-{n}",
            "steps": ["a", "b"],
            "required": True,
        }
        for n in range(1, 7)
    ]
    return Ledger(obligations, [], [], [{"scenario": "flow-3", "steps": ["b"], "why": "cannot"}])


def test_statuses_from_captures(tmp_path: Path) -> None:
    product, ref, out = tmp_path / "indus", tmp_path / "ref", tmp_path / "cmp"
    for d in (product, ref, out):
        d.mkdir()

    def put(folder: Path, flow: str, data: Any, name: str = "capture") -> None:
        (folder / f"{flow}.{name}.json").write_text(json.dumps(data))

    put(product, "flow-1", _capture("flow-1", ["a", "b"]))  # no reference: open
    put(product, "flow-2", _capture("flow-2", ["a"], failed=("b",), status="failed"))  # failed
    put(product, "flow-3", _capture("flow-3", ["a", "b"]))  # gap on b: blocked
    put(product, "flow-4", _capture("flow-4", ["a", "b"]))  # compared, clean: verified
    put(ref, "flow-4", {"steps": []})
    put(out, "flow-4", {"aligned": True}, "compare")
    put(out, "flow-4", [{"kind": "page-errors", "step": None, "class": "intended"}], "divergences")
    put(product, "flow-5", _capture("flow-5", ["a", "b"]))  # unclassified left: open
    put(ref, "flow-5", {"steps": []})
    put(out, "flow-5", {"aligned": True}, "compare")
    put(
        out,
        "flow-5",
        [{"kind": "role-only-in-indus", "step": "a", "class": "unclassified"}],
        "divergences",
    )
    # flow-6 has no product capture: not run
    found = ledger.statuses(_small(), product_dir=product, ref_dir=ref, compare_dir=out)
    assert {k: v[0] for k, v in found.items()} == {
        "OBL-T-001": "open",
        "OBL-T-002": "failed",
        "OBL-T-003": "blocked",
        "OBL-T-004": "verified",
        "OBL-T-005": "open",
        "OBL-T-006": "not run",
    }
    assert found["OBL-T-001"][1].startswith("reference capture owed")
    assert found["OBL-T-002"][1] == "b: failed"


def test_ledger_report_prints_counts_per_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ledger.main(["--ledger", "--cache", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert first.startswith("ledger: verified 0, open 0, blocked 0, failed 0, not run ")
    assert "OBL-CLI-001" in out


def test_check_mode_reports_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert ledger.main(["--check"]) == 0
    assert "ledger self-test: ok" in capsys.readouterr().out
