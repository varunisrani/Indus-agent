"""P22-11: compressor settings (sections, strict keys, shipped defaults) and the tokenizer."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.research import compress_config
from indus.research.compress_config import (
    CompressionConfig,
    ConfigError,
    TokenCounter,
    TokenizerError,
    load_config,
    load_tokenizer,
)
from tests.fakes.research.fake_tokenizer import REFUSE_MARK, FakeTokenizer, RecordingLoader


def test_defaults_table() -> None:
    c = CompressionConfig()
    assert c.tokenizer.name == "Qwen/Qwen3-8B"
    assert c.tokenizer.revision == "b968826d9c46dd6066d109eabc6255188de91218"
    assert c.tokenizer.trust_remote_code is False
    assert (c.compression.target_max_tokens, c.compression.summary_target_tokens) == (15250, 750)
    p = c.protected_turns
    assert (p.first_system, p.first_human, p.first_gpt, p.first_tool, p.last_n) == (
        True,
        True,
        True,
        True,
        4,
    )
    s = c.summarization
    assert s.model is None and s.base_url is None and s.api_key_secret is None
    assert (s.temperature, s.max_retries, s.retry_delay) == (0.3, 3, 2.0)
    assert c.output.add_summary_notice is True and c.output.suffix == "_compressed"
    assert c.output.notice_text.startswith("\n\n")
    assert c.processing.max_concurrent_requests == 50
    assert c.processing.per_trajectory_timeout == 300.0
    assert c.processing.skip_under_target is True
    assert (c.metrics.enabled, c.metrics.per_trajectory) == (True, True)
    assert c.metrics.file == "compression_metrics.json"


def test_shipped_file_equals_the_defaults() -> None:
    assert load_config() == CompressionConfig()


@pytest.mark.parametrize(
    "text",
    [
        "num_workers: 4\n",
        "processing:\n  num_workers: 4\n",
        "processing:\n  save_over_limit: true\n",
        "tokenizer:\n  name_typo: x\n",
        "metrics:\n  output_file: m.json\n",
    ],
)
def test_unknown_keys_are_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=str(path)):
        load_config(path)


def test_partial_file_keeps_other_defaults_and_timeout_is_loadable(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "compression:\n  target_max_tokens: 900\nprocessing:\n  per_trajectory_timeout: 12\n",
        encoding="utf-8",
    )
    c = load_config(path)
    assert c.compression.target_max_tokens == 900
    assert c.compression.summary_target_tokens == 750
    assert c.processing.per_trajectory_timeout == 12.0  # R10.9: loadable
    empty = tmp_path / "e.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_config(empty) == CompressionConfig()


@pytest.mark.parametrize(
    "text",
    [
        "compression:\n  target_max_tokens: 0\n",
        "protected_turns:\n  last_n: -1\n",
        "processing:\n  max_concurrent_requests: 0\n",
        "summarization:\n  max_retries: 0\n",
        "compression: 5\n",
        "- a list\n",
        "tokenizer: [unclosed\n",
    ],
)
def test_bad_values_are_errors(tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "absent.yaml")


def test_remote_code_is_never_passed_true_by_default() -> None:
    loader = RecordingLoader()
    warnings: list[str] = []
    counter = load_tokenizer(CompressionConfig().tokenizer, loader=loader, warn=warnings.append)
    assert loader.calls == [
        (
            "Qwen/Qwen3-8B",
            {"revision": "b968826d9c46dd6066d109eabc6255188de91218", "trust_remote_code": False},
        )
    ]
    assert warnings == []
    assert counter.count("a b c") == 3


def test_opting_in_warns_naming_repo_and_revision() -> None:
    loader = RecordingLoader()
    warnings: list[str] = []
    settings = CompressionConfig().tokenizer.model_copy(
        update={"name": "acme/tok", "revision": "abc123", "trust_remote_code": True}
    )
    load_tokenizer(settings, loader=loader, warn=warnings.append)
    assert loader.calls[0][1]["trust_remote_code"] is True
    [message] = warnings
    assert "acme/tok" in message and "abc123" in message


def test_load_failure_is_fatal() -> None:
    loader = RecordingLoader(fail=OSError("no such repository"))
    with pytest.raises(TokenizerError, match="no such repository"):
        load_tokenizer(CompressionConfig().tokenizer, loader=loader)


def test_missing_transformers_is_a_tokenizer_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_transformers(name: str, *args: object, **kwargs: object) -> object:
        if name == "transformers":
            raise ImportError("No module named 'transformers'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", no_transformers)
    with pytest.raises(TokenizerError, match="research extra"):
        load_tokenizer(CompressionConfig().tokenizer)


def test_encode_error_falls_back_to_length_over_four() -> None:
    counter = TokenCounter(FakeTokenizer())
    assert counter.count("") == 0
    assert counter.count("one two") == 2
    text = "x" * 40 + REFUSE_MARK
    assert counter.count(text) == len(text) // 4
    assert counter.fallbacks == 1


def test_module_has_no_import_time_tokenizer() -> None:
    assert not hasattr(compress_config, "AutoTokenizer")
