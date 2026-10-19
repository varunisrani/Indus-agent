"""P22-13: ``indus research compress`` file and folder runs with a fake summariser and the
whitespace tokenizer (ACC-O6-1, ACC-O6-3; the M8 demo's compress half)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from indus.cli.commands import research as research_cmd
from indus.foundation.scope import Scope
from indus.research.compress_config import CompressionConfig, TokenCounter
from indus.research.compress_runner import RunSettings, run, sample_count
from indus.research.compressor import SUMMARY_MARKER, SummaryReply
from tests.fakes.research.fake_tokenizer import FakeTokenizer, RecordingLoader, words
from tests.unit.cli.commands.cmdrun import run_cli

HANG = "HANG-HERE"


class FakeSummariser:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, prompt: str, *, max_tokens: int, temperature: float) -> SummaryReply:
        self.calls += 1
        if HANG in prompt:
            await asyncio.Event().wait()  # never answers: the per-entry deadline must fire
        return SummaryReply(f"{SUMMARY_MARKER} the assistant listed files and read two", "stop")


def cfg(**processing: Any) -> CompressionConfig:
    return CompressionConfig.model_validate(
        {
            "compression": {"target_max_tokens": 100, "summary_target_tokens": 10},
            "processing": {"per_trajectory_timeout": 0.5, **processing},
        }
    )


def trajectory(tag: str, middle: int = 60) -> dict[str, Any]:
    turns = [
        {"from": "system", "value": words(5, "sys")},
        {"from": "human", "value": words(5, "ask")},
        {"from": "gpt", "value": words(5, "call")},
        {"from": "tool", "value": words(5, "res")},
        {"from": "gpt", "value": words(middle, tag)},
        {"from": "tool", "value": words(middle, tag)},
        {"from": "gpt", "value": words(middle, tag)},
        {"from": "human", "value": "more"},
        {"from": "gpt", "value": "ok"},
        {"from": "human", "value": "thanks"},
        {"from": "gpt", "value": "bye"},
    ]
    return {"conversations": turns, "id": tag}


def small(tag: str) -> dict[str, Any]:
    return {"conversations": [{"from": "human", "value": "hi"}], "id": tag}


def write_jsonl(path: Path, rows: list[Any]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def read_jsonl(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def go(settings: RunSettings, config: CompressionConfig, summariser: FakeSummariser) -> int:
    lines: list[str] = []
    return run(
        settings,
        config,
        counter=lambda: TokenCounter(FakeTokenizer()),
        make_summariser=lambda: summariser,
        out=lines.append,
        err=lines.append,
    )


def test_file_mode_hanging_entry_goes_to_rejects_and_order_is_kept(tmp_path: Path) -> None:
    source = write_jsonl(
        tmp_path / "runs.jsonl",
        [trajectory("a"), small("b"), trajectory(HANG), "not an object", trajectory("d")],
    )
    with source.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    summariser = FakeSummariser()
    assert go(RunSettings(source), cfg(), summariser) == 0
    out = read_jsonl(tmp_path / "runs_compressed.jsonl")
    assert [r["id"] if isinstance(r, dict) else r for r in out] == ["a", "b", "not an object", "d"]
    assert out[0]["conversations"][4]["value"].startswith(SUMMARY_MARKER)
    assert "compression_metrics" in out[0] and "compression_metrics" not in out[1]
    [reject] = read_jsonl(tmp_path / "runs_compressed.rejects.jsonl")
    assert reject["reason"] == "timeout" and reject["line"] == 3
    assert reject["source_file"] == str(source) and reject["entry"]["id"] == HANG
    metrics = json.loads((tmp_path / "runs_compressed_metrics.json").read_text(encoding="utf-8"))
    s = metrics["summary"]
    assert (s["total"], s["compressed"], s["skipped_under_target"]) == (5, 2, 1)
    assert (s["timed_out"], s["failed"], s["passed_through"]) == (1, 1, 1)
    t = metrics["tokens"]
    assert t["before"] - t["after"] == t["saved"] and t["saved"] > 0
    assert metrics["turns"]["before"] - metrics["turns"]["after"] == metrics["turns"]["removed"]
    assert set(metrics) == {
        "summary",
        "tokens",
        "turns",
        "averages",
        "summarisation",
        "tokenizer",
        "timings",
    }
    assert metrics["summarisation"]["calls"] == 2  # the hung call never finished counting
    assert metrics["timings"]["started"] and metrics["timings"]["seconds"] >= 0


def test_over_limit_sample_lands_under_target_with_an_empty_rejects_file(tmp_path: Path) -> None:
    source = write_jsonl(tmp_path / "s.jsonl", [trajectory("big", middle=80)])
    assert go(RunSettings(source), cfg(), FakeSummariser()) == 0
    [row] = read_jsonl(tmp_path / "s_compressed.jsonl")
    m = row["compression_metrics"]
    assert m["tokens"]["before"] > 100 >= m["tokens"]["after"]
    assert m["was_compressed"] and not m["still_over_limit"]
    assert (tmp_path / "s_compressed.rejects.jsonl").read_text(encoding="utf-8") == ""
    assert (tmp_path / "s_compressed_metrics.json").exists()


def test_folder_mode_names(tmp_path: Path) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    write_jsonl(folder / "one.jsonl", [trajectory("x"), small("y")])
    write_jsonl(folder / "two.jsonl", [small("z")])
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")
    assert go(RunSettings(folder), cfg(), FakeSummariser()) == 0
    out = tmp_path / "data_compressed"
    assert sorted(p.name for p in out.iterdir()) == [
        "compression_metrics.json",
        "one.jsonl",
        "two.jsonl",
    ]
    assert [r["id"] for r in read_jsonl(out / "one.jsonl")] == ["x", "y"]
    assert (tmp_path / "data_compressed.rejects.jsonl").exists()


@pytest.mark.parametrize(
    ("total", "percent", "kept"),
    [(10, 15, 1), (10, 10, 1), (10, 25, 2), (10, 100, 10), (3, 1, 1), (200, 33.3, 66), (0, 50, 0)],
)
def test_sampling_count_formula(total: int, percent: float, kept: int) -> None:
    assert sample_count(total, percent) == kept


def test_sampling_is_seeded_and_keeps_order(tmp_path: Path) -> None:
    rows = [small(str(i)) for i in range(20)]
    source = write_jsonl(tmp_path / "s.jsonl", rows)
    settings = RunSettings(source, sample_percent=25, seed=42)
    assert go(settings, cfg(), FakeSummariser()) == 0
    first = [r["id"] for r in read_jsonl(tmp_path / "s_compressed.jsonl")]
    assert len(first) == 5 and first == sorted(first, key=int)
    assert go(settings, cfg(), FakeSummariser()) == 0
    assert [r["id"] for r in read_jsonl(tmp_path / "s_compressed.jsonl")] == first


@pytest.mark.parametrize("percent", [0, -5, 100.5])
def test_bad_percent_exits_2(tmp_path: Path, percent: float) -> None:
    source = write_jsonl(tmp_path / "s.jsonl", [small("a")])
    assert go(RunSettings(source, sample_percent=percent), cfg(), FakeSummariser()) == 2


def test_dry_run_writes_nothing_and_loads_no_tokenizer(tmp_path: Path) -> None:
    source = write_jsonl(tmp_path / "s.jsonl", [trajectory("a")])
    before = sorted(p.name for p in tmp_path.iterdir())

    def no_tokenizer() -> TokenCounter:
        raise AssertionError("a dry run loaded the tokenizer")

    code = run(
        RunSettings(source, dry_run=True),
        cfg(),
        counter=no_tokenizer,
        make_summariser=FakeSummariser,
        out=lambda line: None,
        err=lambda line: None,
    )
    assert code == 0 and sorted(p.name for p in tmp_path.iterdir()) == before


def test_left_out_under_target_when_skip_is_off(tmp_path: Path) -> None:
    source = write_jsonl(tmp_path / "s.jsonl", [small("a"), trajectory("b")])
    assert go(RunSettings(source), cfg(skip_under_target=False), FakeSummariser()) == 0
    assert [r["id"] for r in read_jsonl(tmp_path / "s_compressed.jsonl")] == ["b"]


def test_missing_input_exits_2(tmp_path: Path) -> None:
    assert go(RunSettings(tmp_path / "absent.jsonl"), cfg(), FakeSummariser()) == 2


# -- the CLI verb ----------------------------------------------------------------------------------


def test_cli_compress_end_to_end(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = RecordingLoader()
    monkeypatch.setattr(research_cmd, "tokenizer_loader", lambda: loader)
    monkeypatch.setattr(research_cmd, "summariser_for", lambda scope, config: FakeSummariser())
    source = write_jsonl(tmp_path / "s.jsonl", [trajectory("big", middle=80)])
    conf = tmp_path / "c.yaml"
    conf.write_text("compression:\n  summary_target_tokens: 10\n", encoding="utf-8")
    ran = run_cli(
        capsys, scope, "research", "compress", str(source), "--config", str(conf),
        "--target-max-tokens", "100",
    )  # fmt: skip
    assert ran.code == 0, ran.err
    assert loader.calls[0][1]["trust_remote_code"] is False
    [row] = read_jsonl(tmp_path / "s_compressed.jsonl")
    assert row["compression_metrics"]["tokens"]["after"] <= 100
    assert "rejects:" in ran.out


def test_cli_compress_trust_remote_code_warns(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = RecordingLoader()
    monkeypatch.setattr(research_cmd, "tokenizer_loader", lambda: loader)
    monkeypatch.setattr(research_cmd, "summariser_for", lambda scope, config: FakeSummariser())
    source = write_jsonl(tmp_path / "s.jsonl", [small("a")])
    ran = run_cli(
        capsys, scope, "research", "compress", str(source), "--trust-remote-code",
        "--tokenizer", "acme/tok", "--revision", "r1",
    )  # fmt: skip
    assert ran.code == 0
    assert loader.calls == [("acme/tok", {"revision": "r1", "trust_remote_code": True})]
    assert "acme/tok" in ran.err and "r1" in ran.err


def test_cli_compress_usage_errors(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(research_cmd, "tokenizer_loader", lambda: RecordingLoader())
    assert run_cli(capsys, scope, "research", "compress").code == 2
    source = write_jsonl(tmp_path / "s.jsonl", [small("a")])
    bad = tmp_path / "bad.yaml"
    bad.write_text("processing:\n  num_workers: 4\n", encoding="utf-8")
    ran = run_cli(capsys, scope, "research", "compress", str(source), "--config", str(bad))
    assert ran.code == 2 and "num_workers" in ran.err
    ran = run_cli(capsys, scope, "research", "compress", str(source), "--sample-percent", "0")
    assert ran.code == 2
    monkeypatch.setattr(
        research_cmd, "tokenizer_loader", lambda: RecordingLoader(fail=OSError("offline"))
    )
    ran = run_cli(capsys, scope, "research", "compress", str(source))
    assert ran.code == 2 and "offline" in ran.err
