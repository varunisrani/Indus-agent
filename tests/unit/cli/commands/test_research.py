"""P22-09: ``indus research`` (batch flags, distributions, the later verbs)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from indus.cli.commands import research as research_cmd
from indus.foundation.scope import Scope
from indus.research import distributions
from tests.unit.cli.commands.cmdrun import Ran, run_cli


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The settings ``batch`` would run with (the runner itself is replaced)."""
    seen: list[Any] = []

    def fake_run(settings: Any) -> int:
        seen.append(settings)
        return 0

    monkeypatch.setattr(research_cmd, "run_batch", fake_run)
    return seen


@pytest.fixture
def data(tmp_path: Path) -> Path:
    path = tmp_path / "d.jsonl"
    path.write_text(json.dumps({"prompt": "hi"}) + "\n", encoding="utf-8")
    return path


def batch(capsys: pytest.CaptureFixture[str], scope: Scope, data: Path, *args: str) -> Ran:
    return run_cli(capsys, scope, "research", "batch", str(data), "--run-name", "r1", *args)


def test_defaults_are_left_to_the_runner(
    capsys: pytest.CaptureFixture[str], scope: Scope, data: Path, captured: list[Any]
) -> None:
    assert batch(capsys, scope, data).code == 0
    [settings] = captured
    assert settings.dataset == data and settings.run_name == "r1"
    assert settings.batch_size is None and settings.workers is None
    assert settings.reasoning_effort is None  # nothing sent: provider default (R10.4)
    assert settings.routing is None and settings.prefill == ()
    assert settings.resume is False and settings.new_dataset is False


def test_bad_prefill_exits_1(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    data: Path,
    tmp_path: Path,
    captured: list[Any],
) -> None:
    obj = tmp_path / "obj.json"
    obj.write_text('{"role": "user"}', encoding="utf-8")
    ran = batch(capsys, scope, data, "--prefill", str(obj))
    assert ran.code == 1 and "JSON array" in ran.err
    broken = tmp_path / "broken.json"
    broken.write_text("[{", encoding="utf-8")
    assert batch(capsys, scope, data, "--prefill", str(broken)).code == 1
    assert batch(capsys, scope, data, "--prefill", str(tmp_path / "missing.json")).code == 1
    assert captured == []
    good = tmp_path / "good.json"
    good.write_text('[{"role": "user", "content": "x"}]', encoding="utf-8")
    assert batch(capsys, scope, data, "--prefill", str(good)).code == 0
    assert captured[0].prefill == ({"role": "user", "content": "x"},)


def test_bad_reasoning_effort_exits_2(
    capsys: pytest.CaptureFixture[str], scope: Scope, data: Path, captured: list[Any]
) -> None:
    ran = batch(capsys, scope, data, "--reasoning-effort", "ultra")
    assert ran.code == 2 and "medium" in ran.err
    assert captured == []
    assert batch(capsys, scope, data, "--reasoning-effort", "HIGH").code == 0
    assert captured[0].reasoning_effort == "high"


def test_no_reasoning_wins_and_sends_none(
    capsys: pytest.CaptureFixture[str], scope: Scope, data: Path, captured: list[Any]
) -> None:
    ran = batch(capsys, scope, data, "--reasoning-effort", "high", "--no-reasoning")
    assert ran.code == 0
    assert captured[0].reasoning_effort == "none"


def test_routing_strings_are_split_and_trimmed(
    capsys: pytest.CaptureFixture[str], scope: Scope, data: Path, captured: list[Any]
) -> None:
    ran = batch(
        capsys,
        scope,
        data,
        "--providers-allowed",
        " alpha , beta ,, ",
        "--providers-ignored",
        "gamma",
        "--providers-order",
        " , ",
        "--provider-sort",
        "throughput",
    )
    assert ran.code == 0
    assert captured[0].routing == {
        "only": ["alpha", "beta"],
        "ignore": ["gamma"],
        "sort": "throughput",
    }
    assert research_cmd.split_csv(" , ") is None
    assert batch(capsys, scope, data, "--provider-sort", "cheapest").code == 2


def test_usage_errors_exit_2(
    capsys: pytest.CaptureFixture[str], scope: Scope, data: Path, captured: list[Any]
) -> None:
    assert batch(capsys, scope, data, "--batch-size", "0").code == 2
    assert batch(capsys, scope, data, "--workers", "0").code == 2
    assert run_cli(capsys, scope, "research", "batch", str(data)).code == 2  # no --run-name
    assert captured == []


def test_the_real_runner_reports_a_missing_dataset_with_2(
    capsys: pytest.CaptureFixture[str], scope: Scope, tmp_path: Path
) -> None:
    ran = run_cli(
        capsys, scope, "research", "batch", str(tmp_path / "nope.jsonl"), "--run-name", "r1"
    )
    assert ran.code == 2 and "not found" in ran.err


def test_distributions_lists_all_17_sorted(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = run_cli(capsys, scope, "research", "distributions")
    assert ran.code == 0
    heads = [line.split(":")[0] for line in ran.out.splitlines() if line and not line[0].isspace()]
    assert heads == sorted(distributions.names()) and len(heads) == 17
    block = ran.out.split("browser_tasks:", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    assert [line.split()[0] for line in block] == ["browser+search", "terminal", "vision"]


def test_one_distribution_and_an_unknown_one(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = run_cli(capsys, scope, "research", "distributions", "minimal")
    assert ran.code == 0 and ran.out.splitlines()[1].split() == ["web", "100%"]
    ran = run_cli(capsys, scope, "research", "distributions", "nope")
    assert ran.code == 2 and "terminal_only" in ran.err


@pytest.mark.parametrize("verb", ["compress", "swe", "sample"])
def test_later_verbs_say_not_yet(
    capsys: pytest.CaptureFixture[str], scope: Scope, verb: str
) -> None:
    ran = run_cli(capsys, scope, "research", verb, "anything", "--flag")
    assert ran.code == 1
    assert "not available yet" in ran.err
