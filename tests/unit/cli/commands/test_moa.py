"""P10-22 / I28: ``indus moa`` list / configure / delete."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.cli.commands import moa as moa_cmd
from indus.cli.prompts import Interactive
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import EnvView, Scope
from tests.fakes.prompts import scripted


@pytest.fixture
def clean(scope: Scope) -> Scope:
    loader.reset()
    return scope.with_env(EnvView({"INDUS_HOME": str(scope.home)}))


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    queue: list[object] = []

    def factory(state: CliState, guidance: str) -> Interactive:
        if not queue:
            return Interactive(tty=False, guidance=guidance)
        items = list(queue)
        queue.clear()
        return scripted(*items, guidance=guidance)

    monkeypatch.setattr(moa_cmd, "io_factory", factory)
    yield queue


def run(scope: Scope, *args: str) -> int:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    return app_mod.dispatch(["moa", *args], state)


def moa_section(scope: Scope) -> object:
    loader.reset()
    return config.load(scope).get("moa")


def write_config(scope: Scope, text: str) -> None:
    path = loader.config_path(scope)
    path.write_text(text, encoding="utf-8")
    loader.invalidate(path)


def test_list_with_no_presets(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(clean) == 0
    assert run(clean, "ls") == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["No mixture-of-agents presets yet. Make one with: indus moa configure NAME"] * 2


def test_configure_by_flags_keeps_comments(
    clean: Scope, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(clean, "# my own note, keep me\nagent:\n  max_turns: 40  # long runs\n")
    code = run(
        clean,
        "configure",
        "review",
        "--reference",
        "openai:gpt-test",
        "--reference",
        "anthropic:claude-test",
        "--aggregator",
        "openrouter:big",
        "--timeout",
        "90",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Saved MoA preset review:" in out and "It is the default preset" in out
    text = loader.config_path(clean).read_text(encoding="utf-8")
    assert "# my own note, keep me" in text and "max_turns: 40  # long runs" in text
    assert moa_section(clean) == {
        "default_preset": "review",
        "reference_timeout": None,
        "privacy_filter": "",
        "presets": {
            "review": {
                "reference_models": [
                    {"provider": "openai", "model": "gpt-test", "enabled": True},
                    {"provider": "anthropic", "model": "claude-test", "enabled": True},
                ],
                "aggregator": {"provider": "openrouter", "model": "big"},
                "reference_timeout": 90.0,
            }
        },
    }


def test_configure_refuses_and_lists_every_problem(
    clean: Scope, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(
        clean,
        "configure",
        "bad",
        "--reference",
        "nowhere:m",
        "--reference",
        "moa:other",
        "--fanout",
        "x",
    )
    assert code == 1
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "indus moa: preset 'bad' was not saved:",
        "  - preset 'bad' reference 1: no provider is called 'nowhere'",
        "  - preset 'bad' reference 2: 'moa' is a mixture of agents itself; presets cannot be "
        "nested",
        "  - preset 'bad' fanout: must be per_iteration, user_turn or every_n:N (N at least 1)",
    ]
    assert moa_section(clean) is None or moa_section(clean) == {
        "presets": {},
        "default_preset": "",
        "reference_timeout": None,
        "privacy_filter": "",
    }


def test_configure_without_a_terminal_needs_flags(
    clean: Scope, capsys: pytest.CaptureFixture[str], answers: list[object]
) -> None:
    assert run(clean, "configure") == 1
    assert moa_cmd.NEEDS_TERMINAL in capsys.readouterr().err


def test_configure_with_the_picker(
    clean: Scope,
    capsys: pytest.CaptureFixture[str],
    answers: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(moa_cmd, "usable_providers", lambda scope: ["openai", "anthropic"])
    answers.extend(["anthropic", "claude-test", "openai", "gpt-test", "Done", 0])
    assert run(clean, "configure", "pair") == 0
    section = moa_section(clean)
    assert isinstance(section, dict)
    refs = section["presets"]["pair"]["reference_models"]
    assert [(r["provider"], r["model"]) for r in refs] == [
        ("anthropic", "claude-test"),
        ("openai", "gpt-test"),
    ]
    assert "aggregator" not in section["presets"]["pair"]


def test_usable_providers_are_the_configured_ones(clean: Scope) -> None:
    assert "openai" not in moa_cmd.usable_providers(clean)
    keyed = clean.with_env(
        EnvView({"INDUS_HOME": str(clean.home), "OPENAI_API_KEY": "k-" + "a1b2c3d4" * 3})
    )
    assert "openai" in moa_cmd.usable_providers(keyed)


def test_delete_refuses_the_default_preset(
    clean: Scope, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(clean, "configure", "main", "--reference", "openai:a") == 0
    assert run(clean, "configure", "spare", "--reference", "openai:b") == 0
    capsys.readouterr()
    assert run(clean, "delete", "main") == 1
    assert "is the default preset, so it stays" in capsys.readouterr().err
    assert run(clean, "delete", "nope") == 1
    assert "no preset is called 'nope'" in capsys.readouterr().err
    assert run(clean, "rm", "spare") == 0
    assert capsys.readouterr().out == "Deleted MoA preset spare.\n"
    section = moa_section(clean)
    assert isinstance(section, dict) and list(section["presets"]) == ["main"]


def test_list_output(clean: Scope) -> None:
    write_config(
        clean,
        "moa:\n  default_preset: review\n  reference_timeout: 60\n  privacy_filter: display\n"
        "  presets:\n"
        "    review:\n      reference_models:\n"
        "        - {provider: openai, model: m1}\n"
        "        - {provider: anthropic, model: m2, enabled: false}\n"
        "      aggregator: {provider: openai, model: big}\n"
        "    broken:\n      reference_models: []\n",
    )
    state = CliState(scope=clean, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(app_mod.cli, ["moa", "list"], obj=state)
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "* review",
        "    reference 1: openai:m1",
        "    reference 2: anthropic:m2  (disabled)",
        "    aggregator: openai:big",
        "    timeout: 60 s, fan-out: per_iteration",
        "Default preset (*): review",
        "Privacy filter: display",
        "Problems in the moa settings (those presets are not used):",
        "  - preset 'broken': no reference models (add at least one)",
    ]
