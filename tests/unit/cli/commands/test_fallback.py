"""P07-05: ``indus fallback`` list / add / remove / clear."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import respx

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.cli.commands import fallback
from indus.cli.prompts import Interactive
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import EnvView, Scope
from indus.foundation.secrets import SecretStore
from tests.fakes.prompts import CANCEL, scripted

FAKE_KEY = "test" + "-key-" + "0123456789abcdef"


def secrets_of(scope: Scope) -> SecretStore:
    store = scope.secrets
    assert isinstance(store, SecretStore)
    return store


@pytest.fixture
def clean(scope: Scope) -> Scope:
    loader.reset()
    return scope.with_env(EnvView({"INDUS_HOME": str(scope.home)}))


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Answers for the terminal questions (an empty list = no terminal)."""
    queue: list[object] = []

    def factory(state: CliState, guidance: str) -> Interactive:
        if not queue:
            return Interactive(tty=False, guidance=guidance)
        items = list(queue)
        queue.clear()
        return scripted(*items, guidance=guidance)

    monkeypatch.setattr(fallback, "io_factory", factory)
    yield queue


def run(scope: Scope, *args: str) -> int:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    return app_mod.dispatch(["fallback", *args], state)


def chain(scope: Scope) -> object:
    return config.load(scope).get(fallback.KEY)


def test_aliases_and_default(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(clean) == 0
    assert "No fallback routes are set" in capsys.readouterr().out
    assert run(clean, "add", "openrouter", "a/one") == 0
    capsys.readouterr()
    for form in ([], ["list"], ["ls"]):
        assert run(clean, *form) == 0
        assert "1. a/one  via openrouter" in capsys.readouterr().out
    assert run(clean, "rm", "1") == 0
    assert chain(clean) == []
    assert run(clean, "lst") == 2
    assert "Did you mean: list" in capsys.readouterr().err


def test_add_remove_clear_round_trip(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    path = config.config_path(clean)
    config.save(clean, {"terminal": {"timeout": 99}})
    path.write_text("# my notes\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert run(clean, "add", "or", "a/one") == 0  # an alias resolves to the canonical name
    assert run(clean, "add", "deepseek", "deepseek-chat") == 0
    assert chain(clean) == [
        {"provider": "openrouter", "model": "a/one"},
        {"provider": "deepseek", "model": "deepseek-chat"},
    ]
    assert "The chain has 2 entries." in capsys.readouterr().out
    assert run(clean, "remove", "1") == 0
    assert chain(clean) == [{"provider": "deepseek", "model": "deepseek-chat"}]
    assert run(clean, "clear", "-y") == 0
    assert chain(clean) == []
    text = path.read_text(encoding="utf-8")
    assert "# my notes" in text and "timeout: 99" in text


def test_add_same_as_primary_or_duplicate(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    config.save(clean, {"model": {"provider": "deepseek", "default": "deepseek-chat"}})
    assert run(clean, "add", "deepseek", "deepseek-chat") == 0
    assert "cannot be its own fallback" in capsys.readouterr().out
    assert chain(clean) == []
    assert run(clean, "add", "xai", "grok-x") == 0
    assert run(clean, "add", "xai", "grok-x") == 0
    assert "already in the chain" in capsys.readouterr().out
    assert chain(clean) == [{"provider": "xai", "model": "grok-x"}]


def test_malformed_entries_are_reported(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    config.save(
        clean,
        {"fallback_providers": [{"provider": "xai"}, "oops", {"provider": "xai", "model": "g"}]},
    )
    assert run(clean, "list") == 0
    out = capsys.readouterr().out
    assert "1. {'provider': 'xai'}  (ignored: missing model)" in out
    assert "(ignored: not a {provider, model} entry)" in out
    assert "3. g  via xai" in out
    assert "2 entries cannot be used" in out
    config.save(clean, {"fallback_providers": {"provider": "xai"}})
    assert run(clean, "list") == 0
    assert "is not a list" in capsys.readouterr().out
    assert run(clean, "add", "xai", "g") == 0
    assert chain(clean) == [{"provider": "xai", "model": "g"}]


def test_non_tty_argument_forms(
    clean: Scope, answers: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(clean, "add") == 1
    assert "indus fallback add PROVIDER MODEL" in capsys.readouterr().err
    assert run(clean, "add", "openrouter") == 2
    assert run(clean, "add", "nosuch", "m") == 1
    assert "no provider is called 'nosuch'" in capsys.readouterr().err
    assert run(clean, "remove") == 0  # nothing to remove is not an error
    assert run(clean, "add", "xai", "g") == 0
    assert run(clean, "remove") == 1
    assert "indus fallback remove INDEX" in capsys.readouterr().err
    assert run(clean, "remove", "5") == 1
    assert run(clean, "remove", "x") == 1
    assert run(clean, "clear") == 1
    assert "clear -y" in capsys.readouterr().err
    assert chain(clean) == [{"provider": "xai", "model": "g"}]


def test_add_with_picker_keeps_primary(clean: Scope, answers: list[object]) -> None:
    config.save(clean, {"model": {"provider": "xai", "default": "grok-x"}})
    before = config.config_path(clean).read_text(encoding="utf-8")
    with respx.mock(assert_all_called=False) as router:
        router.get("https://openrouter.ai/api/v1/models").respond(
            200, json={"data": [{"id": "a/one"}, {"id": "b/two"}]}
        )
        answers.extend(["OpenRouter", "Paste an API key", FAKE_KEY, "b/two"])
        assert run(clean, "add") == 0
    raw = config.load(clean).user_raw()
    assert raw["model"] == {"provider": "xai", "default": "grok-x"}
    assert raw["fallback_providers"] == [{"provider": "openrouter", "model": "b/two"}]
    assert secrets_of(clean).get("OPENROUTER_API_KEY") == FAKE_KEY
    assert "agent" not in raw  # no reasoning question for a fallback pick
    assert before != config.config_path(clean).read_text(encoding="utf-8")


def test_add_with_picker_cancelled(
    clean: Scope, answers: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    answers.extend(["Leave unchanged"])
    assert run(clean, "add") == 0
    answers.extend([CANCEL])
    assert run(clean, "add") == 0
    assert chain(clean) is None or chain(clean) == []


def test_remove_with_picker(
    clean: Scope, answers: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(clean, "add", "xai", "g1") == 0
    assert run(clean, "add", "xai", "g2") == 0
    answers.extend(["Cancel"])
    assert run(clean, "remove") == 0
    assert "No change." in capsys.readouterr().out
    answers.extend(["g2"])
    assert run(clean, "remove") == 0
    assert chain(clean) == [{"provider": "xai", "model": "g1"}]


def test_clear_asks_on_a_terminal(
    clean: Scope, answers: list[object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(clean, "add", "xai", "g1") == 0
    answers.extend([""])
    assert run(clean, "clear") == 0
    assert chain(clean) == [{"provider": "xai", "model": "g1"}]
    answers.extend(["y"])
    assert run(clean, "clear") == 0
    assert chain(clean) == []
    assert run(clean, "clear") == 0
    assert "nothing to clear" in capsys.readouterr().out
