"""P22-15: ``indus research sample`` (required dataset list, token threshold, seeded draw,
compress and merge). ACC-O7-1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from indus.cli.commands import research as research_cmd
from indus.foundation.scope import Scope
from indus.research.compress_config import CompressionConfig, TokenCounter
from indus.research.compress_runner import RunSettings, run
from indus.research.compressor import SUMMARY_MARKER, SummaryReply
from indus.research.sample import (
    SampleError,
    SampleSettings,
    dataset_list,
    draw,
    run_sample,
)
from tests.fakes.research.fake_tokenizer import FakeTokenizer, RecordingLoader, words
from tests.unit.cli.commands.cmdrun import run_cli, write_settings


def row(tag: str, size: int) -> dict[str, Any]:
    turns = [
        {"from": "system", "value": "sys"},
        {"from": "human", "value": "ask"},
        {"from": "gpt", "value": "call"},
        {"from": "tool", "value": "res"},
        {"from": "gpt", "value": words(size, tag)},
        {"from": "human", "value": words(size, tag)},
        {"from": "gpt", "value": "a"},
        {"from": "human", "value": "b"},
        {"from": "gpt", "value": "c"},
        {"from": "human", "value": "d"},
    ]
    return {"id": tag, "conversations": turns}


DATA = {
    "acme/long-runs": [row("a1", 50), row("a2", 5), row("a3", 60)],
    "acme/more-runs": [row("b1", 70), row("b2", 1), {"id": "b3", "text": "no conversations"}],
}


def loader(name: str) -> list[dict[str, Any]]:
    return [dict(r) for r in DATA[name]]


class Recap:
    async def __call__(self, prompt: str, *, max_tokens: int, temperature: float) -> SummaryReply:
        return SummaryReply(f"{SUMMARY_MARKER} recap", "stop")


def counter() -> TokenCounter:
    return TokenCounter(FakeTokenizer())


CONFIG = CompressionConfig.model_validate(
    {"compression": {"target_max_tokens": 60, "summary_target_tokens": 5}}
)


def compress(source: Path, target: Path) -> int:
    return run(
        RunSettings(source, target),
        CONFIG,
        counter=counter,
        make_summariser=Recap,
        out=lambda line: None,
        err=lambda line: None,
    )


def test_no_dataset_list_is_refused() -> None:
    with pytest.raises(SampleError, match="no default dataset"):
        dataset_list((), None)
    with pytest.raises(SampleError):
        dataset_list(("  ",), [])
    assert dataset_list((), ["x/y", "x/y", " z "]) == ("x/y", "z")
    assert dataset_list(("given",), ["configured"]) == ("given",)


def test_draw_keeps_long_rows_and_samples_with_the_seed(tmp_path: Path) -> None:
    lines: list[str] = []
    settings = SampleSettings(tuple(DATA), tmp_path, samples=10, min_tokens=100)
    pool = draw(settings, loader, counter(), lines.append)
    assert [r["id"] for r in pool] == ["a1", "a3", "b1"]
    assert pool[0]["sampled_from"] == "acme/long-runs" and pool[0]["sampled_tokens"] >= 100
    small = SampleSettings(tuple(DATA), tmp_path, samples=2, min_tokens=100, seed=42)
    first = [r["id"] for r in draw(small, loader, counter(), lines.append)]
    again = [r["id"] for r in draw(small, loader, counter(), lines.append)]
    assert len(first) == 2 and first == again


def test_run_sample_compresses_and_merges(tmp_path: Path) -> None:
    settings = SampleSettings(tuple(DATA), tmp_path, name="set", samples=10, min_tokens=100)
    lines: list[str] = []
    code = run_sample(
        settings, loader=loader, counter=counter, compress=compress, out=lines.append,
        err=lines.append,
    )  # fmt: skip
    assert code == 0, lines
    merged = [json.loads(x) for x in (tmp_path / "set.jsonl").read_text("utf-8").splitlines()]
    assert [r["id"] for r in merged] == ["a1", "a3", "b1"]
    assert all(r["compression_metrics"]["was_compressed"] for r in merged)
    assert (tmp_path / "set.parts" / "compressed" / "compression_metrics.json").exists()


def test_nothing_over_the_threshold_exits_1(tmp_path: Path) -> None:
    settings = SampleSettings(tuple(DATA), tmp_path, min_tokens=10_000)
    code = run_sample(settings, loader=loader, counter=counter, compress=compress,
                      out=lambda line: None, err=lambda line: None)  # fmt: skip
    assert code == 1


def test_cli_sample_without_a_dataset_list_exits_2(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = run_cli(capsys, scope, "research", "sample")
    assert ran.code == 2 and "no default dataset" in ran.err


def test_cli_sample_with_configured_datasets(
    capsys: pytest.CaptureFixture[str],
    scope: Scope,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(research_cmd, "dataset_loader", lambda: loader)
    monkeypatch.setattr(research_cmd, "tokenizer_loader", lambda: RecordingLoader())
    monkeypatch.setattr(research_cmd, "summariser_for", lambda scope, config: Recap())
    write_settings(scope, "research:\n  sample_datasets: [acme/long-runs, acme/more-runs]\n")
    conf = tmp_path / "c.yaml"
    conf.write_text(
        "compression:\n  target_max_tokens: 60\n  summary_target_tokens: 5\n", encoding="utf-8"
    )
    ran = run_cli(
        capsys, scope, "research", "sample", "--min-tokens", "100", "--out", str(tmp_path),
        "--name", "picked", "--config", str(conf), "--samples", "2",
    )  # fmt: skip
    assert ran.code == 0, ran.err
    merged = (tmp_path / "picked.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(merged) == 2
