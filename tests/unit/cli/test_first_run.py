"""P07-03: the first-run guard and the explicit-route guard."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from indus.cli import app as app_mod
from indus.cli import first_run
from indus.cli.args import CliState, GlobalArgs
from indus.cli.first_run import SelectionWarning, first_run_guard, route_guard, usable_provider
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
    """The profile scope with an empty env view (no keys inherited from the machine)."""
    loader.reset()
    return scope.with_env(EnvView({"INDUS_HOME": str(scope.home)}))


def state_for(scope: Scope, *, tty: bool) -> CliState:
    return CliState(scope=scope, stdin_isatty=lambda: tty, stdout_isatty=lambda: tty)


def _env(name: str, value: str) -> Callable[[Scope], Scope]:
    return lambda s: s.with_env(s.env.with_overlay({name: value}))


def _stored(name: str) -> Callable[[Scope], Scope]:
    def apply(s: Scope) -> Scope:
        secrets_of(s).set(name, FAKE_KEY)
        return s

    return apply


def _dotenv(s: Scope) -> Scope:
    (s.home / ".env").write_text(f"XAI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    return s


def _saved(data: dict[str, object]) -> Callable[[Scope], Scope]:
    def apply(s: Scope) -> Scope:
        config.save(s, data)
        return s

    return apply


def _auth(doc: dict[str, object]) -> Callable[[Scope], Scope]:
    def apply(s: Scope) -> Scope:
        (s.home / first_run.AUTH_FILE).write_text(json.dumps(doc), encoding="utf-8")
        return s

    return apply


@pytest.mark.parametrize(
    ("name", "prepare", "expected"),
    [
        ("env key", _env("OPENROUTER_API_KEY", FAKE_KEY), "OPENROUTER_API_KEY"),
        ("generic key", _env("OPENAI_API_KEY", FAKE_KEY), "OPENAI_API_KEY"),
        ("secret store", _stored("DEEPSEEK_API_KEY"), "DEEPSEEK_API_KEY"),
        (".env file", _dotenv, "XAI_API_KEY in .env"),
        ("local url env", _env("OPENAI_BASE_URL", "http://127.0.0.1:8080/v1"), "OPENAI_BASE_URL"),
        ("local server config", _saved({"model": {"provider": "ollama"}}), "provider ollama"),
        (
            "local base url",
            _saved({"model": {"base_url": "http://localhost:1234/v1"}}),
            "configured provider custom",
        ),
        ("signed in", _auth({"version": 1, "providers": {"openrouter": {"x": 1}}}), "openrouter"),
        (
            "oauth pool entry",
            _auth({"version": 1, "pool": {"anthropic": [{"auth_type": "oauth"}]}}),
            "anthropic",
        ),
    ],
)
def test_usable_signals(
    clean: Scope, name: str, prepare: Callable[[Scope], Scope], expected: str
) -> None:
    assert usable_provider(clean) is None
    found = usable_provider(prepare(clean))
    assert found is not None and expected in found, name


@pytest.mark.parametrize("value", ["sk-or-", "sk-", "your-api-key", "<paste key>", "xxxxxxxxxx"])
def test_placeholder_key_ignored(clean: Scope, value: str) -> None:
    assert usable_provider(_env("OPENROUTER_API_KEY", value)(clean)) is None
    (clean.home / ".env").write_text(f"OPENROUTER_API_KEY={value}\n", encoding="utf-8")
    assert usable_provider(clean) is None


def test_odd_auth_file_is_no_signal(clean: Scope) -> None:
    (clean.home / first_run.AUTH_FILE).write_text("{broken", encoding="utf-8")
    assert usable_provider(clean) is None
    (clean.home / first_run.AUTH_FILE).write_text('{"pool": {"x": [{"auth_type": "api_key"}]}}')
    assert usable_provider(clean) is None


def test_usable_provider_skips_guard(clean: Scope) -> None:
    ready = _env("OPENROUTER_API_KEY", FAKE_KEY)(clean)
    assert first_run_guard(state_for(ready, tty=False)) is None


def test_non_tty_exits_1_with_guidance(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    called: list[bool] = []
    code = first_run_guard(state_for(clean, tty=False), run_setup=lambda _s: called.append(True))  # type: ignore[arg-type, return-value]
    captured = capsys.readouterr()
    assert code == 1 and not called
    assert "not set up yet" in captured.err and "indus config set model.provider" in captured.err
    assert "indus auth add" in captured.err
    assert captured.out == ""


def test_tty_answers(clean: Scope) -> None:
    state = state_for(clean, tty=True)

    def setup_that_works(st: CliState) -> int:
        secrets_of(st.scope).set("OPENROUTER_API_KEY", FAKE_KEY)
        return 0

    lines: list[str] = []
    assert first_run_guard(state, io=scripted("n", lines=lines), run_setup=setup_that_works) == 1
    assert any("indus setup" in line for line in lines)
    assert first_run_guard(state, io=scripted(CANCEL), run_setup=setup_that_works) == 1
    assert first_run_guard(state, io=scripted("y"), run_setup=lambda _s: 0) == 1
    assert first_run_guard(state, io=scripted("y"), run_setup=lambda _s: 1) == 1
    assert first_run_guard(state, io=scripted(""), run_setup=setup_that_works) is None


# -- route guard --------------------------------------------------------------------------------


def _flag(kind: str) -> first_run.Guard:
    def guard(scope: Scope, provider: str, model: str) -> SelectionWarning | None:
        if "pricey" in model:
            return SelectionWarning(kind, f"{model} is flagged ({kind})")
        return None

    return guard


@pytest.fixture
def guards(monkeypatch: pytest.MonkeyPatch) -> list[first_run.Guard]:
    found: list[first_run.Guard] = []
    monkeypatch.setattr(first_run, "GUARDS", found)
    return found


def test_route_guard_table(
    clean: Scope, guards: list[first_run.Guard], capsys: pytest.CaptureFixture[str]
) -> None:
    guards.append(_flag("cost"))
    tty, pipe = state_for(clean, tty=True), state_for(clean, tty=False)
    flagged = GlobalArgs(model="pricey-1")
    assert route_guard(pipe, GlobalArgs()) is None
    assert route_guard(pipe, GlobalArgs(model="cheap")) is None
    assert route_guard(tty, flagged, io=scripted("y")) is None
    assert route_guard(tty, flagged, io=scripted("")) == 1
    assert route_guard(tty, flagged, io=scripted(CANCEL)) == 1
    capsys.readouterr()
    assert route_guard(pipe, GlobalArgs(model="pricey-1", yolo=True)) == 1
    err = capsys.readouterr().err
    assert "flagged (cost)" in err and "Refused" in err
    # --provider alone: the saved model is the one checked; no model anywhere = nothing to check.
    assert route_guard(pipe, GlobalArgs(provider="openrouter")) is None
    config.save(clean, {"model": {"default": "pricey-2"}})
    assert route_guard(pipe, GlobalArgs(provider="openrouter")) == 1


def test_data_policy_setting_for_unattended_runs(
    clean: Scope, guards: list[first_run.Guard], capsys: pytest.CaptureFixture[str]
) -> None:
    guards.append(_flag("data_policy"))
    pipe = state_for(clean, tty=False)
    flagged = GlobalArgs(model="pricey-1")
    assert route_guard(pipe, flagged) == 1
    assert first_run.ALLOW_DATA_POLICY_KEY in capsys.readouterr().err
    config.save(clean, {"security": {"allow_data_training_tiers_noninteractive": True}})
    assert route_guard(pipe, flagged) is None
    assert "Going ahead without a terminal" in capsys.readouterr().err
    guards.append(_flag("cost"))
    assert route_guard(pipe, flagged) == 1


def test_failing_guard_is_skipped(clean: Scope, guards: list[first_run.Guard]) -> None:
    def broken(scope: Scope, provider: str, model: str) -> SelectionWarning | None:
        raise RuntimeError("boom")

    guards.append(broken)
    assert route_guard(state_for(clean, tty=False), GlobalArgs(model="pricey-1")) is None


def test_builtin_rule_guard_matches_provider_and_model(clean: Scope) -> None:
    rule = first_run.FlaggedRoute("data_policy", "openrouter", "FREE", "trains on prompts")
    guard = first_run._rule_guard([rule])  # pyright: ignore[reportPrivateUsage]
    assert guard(clean, "openrouter", "x/y:free") == SelectionWarning(
        "data_policy", "trains on prompts"
    )
    assert guard(clean, "deepseek", "x/y:free") is None
    assert guard(clean, "openrouter", "x/y") is None


# -- chat wiring --------------------------------------------------------------------------------


def test_chat_runs_the_first_run_guard(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    code = app_mod.dispatch(["chat", "-q", "hello"], state_for(clean, tty=False))
    assert code == 1
    assert "not set up yet" in capsys.readouterr().err
    assert not (clean.home / "state.db").exists()


def test_chat_runs_the_route_guard(
    clean: Scope, guards: list[first_run.Guard], capsys: pytest.CaptureFixture[str]
) -> None:
    guards.append(_flag("cost"))
    ready = _env("OPENROUTER_API_KEY", FAKE_KEY)(clean)
    code = app_mod.dispatch(["-m", "pricey-9", "chat", "-q", "hello"], state_for(ready, tty=False))
    assert code == 1
    assert "flagged (cost)" in capsys.readouterr().err


def test_guidance_mentions_both_paths() -> None:
    from indus.cli.setup.wizard import non_interactive_guidance

    text = non_interactive_guidance()
    assert "indus config set model.base_url" in text and "indus auth add" in text
