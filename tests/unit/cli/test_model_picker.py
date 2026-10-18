"""P07-04: the ``indus model`` picker and its model-list cache."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
import respx

from indus.cli import model_cache, model_picker
from indus.cli.model_picker import PickerDeps, Selection, active_provider, apply, run_model
from indus.cli.prompts import Interactive
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import EnvView, Scope
from indus.foundation.secrets import SecretStore
from indus.providers.registry import Registry
from tests.fakes.clock import FakeClock
from tests.fakes.prompts import CANCEL, ScriptedDriver, scripted

FAKE_KEY = "test" + "-key-" + "0123456789abcdef"
NEW_KEY = "test" + "-key-" + "fedcba9876543210"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
DEEPSEEK_MODELS = "https://api.deepseek.com/v1/models"


def secrets_of(scope: Scope) -> SecretStore:
    store = scope.secrets
    assert isinstance(store, SecretStore)
    return store


@pytest.fixture
def clean(scope: Scope) -> Scope:
    loader.reset()
    return scope.with_env(EnvView({"INDUS_HOME": str(scope.home)}))


@pytest.fixture
def mocked() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(OPENROUTER_MODELS).respond(
            200, json={"data": [{"id": "a/one"}, {"id": "b/two"}]}
        )
        router.get(DEEPSEEK_MODELS).respond(200, json={"data": [{"id": "deepseek-chat"}]})
        yield router


def run(scope: Scope, *answers: object, lines: list[str] | None = None) -> int:
    io = scripted(*answers, lines=lines)
    return run_model(scope, io, deps=PickerDeps(revalidate=lambda job: job()))


def saved(scope: Scope) -> dict[str, object]:
    return config.load(scope).user_raw()


def test_live_list_pick_saves_everything(clean: Scope, mocked: respx.MockRouter) -> None:
    lines: list[str] = []
    code = run(clean, "OpenRouter", "Paste an API key", FAKE_KEY, "b/two", "high", lines=lines)
    assert code == 0
    raw = saved(clean)
    assert raw["model"] == {"default": "b/two", "provider": "openrouter"}
    assert raw["agent"] == {"reasoning_effort": "high"}
    assert secrets_of(clean).get("OPENROUTER_API_KEY") == FAKE_KEY
    sent = mocked.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert "Current model:    (not set)" in lines
    assert any("Saved OPENROUTER_API_KEY" in line for line in lines)
    cache = json.loads(model_cache.cache_path(clean).read_text(encoding="utf-8"))
    assert list(cache["rows"]) == ["openrouter|https://openrouter.ai/api/v1"]
    assert FAKE_KEY not in model_cache.cache_path(clean).read_text(encoding="utf-8")
    assert FAKE_KEY not in config.config_path(clean).read_text(encoding="utf-8")


def test_cancel_writes_nothing(clean: Scope, mocked: respx.MockRouter) -> None:
    lines: list[str] = []
    assert run(clean, "Leave unchanged", lines=lines) == 0
    assert run(clean, "OpenRouter", "Paste an API key", "", lines=lines) == 0
    assert run(clean, "OpenRouter", "Paste an API key", FAKE_KEY, CANCEL, lines=lines) == 0
    assert run(clean, "OpenRouter", "Paste an API key", FAKE_KEY, "a/one", CANCEL, lines=lines) == 0
    assert lines.count("No change.") == 4
    assert not config.config_path(clean).exists()
    assert secrets_of(clean).get("OPENROUTER_API_KEY") is None


def test_openrouter_offers_the_documented_key_mint() -> None:
    assert model_picker.sign_in_for("openrouter") is not None
    assert model_picker.sign_in_for("deepseek") is None


def test_sign_in_stores_the_minted_key(
    clean: Scope, mocked: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    minted = "test" + "-minted-" + "0123456789abcdef"
    monkeypatch.setitem(model_picker.SIGN_IN_FLOWS, "openrouter", lambda scope, io: minted)
    assert run(clean, "OpenRouter", "Sign in", "a/one", "Keep") == 0
    assert secrets_of(clean).get("OPENROUTER_API_KEY") == minted
    assert mocked.calls.last.request.headers["Authorization"] == f"Bearer {minted}"


def test_failed_sign_in_changes_nothing(clean: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []
    monkeypatch.setitem(model_picker.SIGN_IN_FLOWS, "openrouter", lambda scope, io: None)
    assert run(clean, "OpenRouter", "Sign in", lines=lines) == 0
    assert "No change." in lines
    assert not config.config_path(clean).exists()


def test_existing_key_kept_or_replaced(clean: Scope, mocked: respx.MockRouter) -> None:
    secrets_of(clean).set("OPENROUTER_API_KEY", FAKE_KEY)
    assert run(clean, "OpenRouter", "Keep it", "a/one", "Keep") == 0
    assert secrets_of(clean).get("OPENROUTER_API_KEY") == FAKE_KEY
    assert run(clean, "OpenRouter", "Enter a new key", NEW_KEY, "a/one", "Keep") == 0
    assert secrets_of(clean).get("OPENROUTER_API_KEY") == NEW_KEY
    assert mocked.calls.last.request.headers["Authorization"] == f"Bearer {NEW_KEY}"


def test_custom_to_named_clears_stale_route(clean: Scope, mocked: respx.MockRouter) -> None:
    config.save(
        clean,
        {
            "model": {
                "provider": "custom",
                "default": "local-model",
                "base_url": "http://127.0.0.1:9/v1",
                "api_mode": "chat_completions",
            }
        },
    )
    env_file = clean.home / ".env"
    env_file.write_text("OPENAI_BASE_URL=http://127.0.0.1:9/v1\nOTHER=1\n", encoding="utf-8")
    lines: list[str] = []
    assert run(clean, "DeepSeek", FAKE_KEY, "deepseek-chat", "Keep", lines=lines) == 0
    assert saved(clean)["model"] == {"default": "deepseek-chat", "provider": "deepseek"}
    assert env_file.read_text(encoding="utf-8") == "OTHER=1\n"
    assert any("Dropped model.base_url" in line for line in lines)
    assert any("stale OPENAI_BASE_URL" in line for line in lines)


def test_custom_endpoint_detects_its_only_model(clean: Scope, mocked: respx.MockRouter) -> None:
    mocked.get("http://127.0.0.1:8000/v1/models").respond(200, json={"data": [{"id": "qwen"}]})
    env_file = clean.home / ".env"
    env_file.write_text("OPENAI_BASE_URL=http://127.0.0.1:8000/v1\n", encoding="utf-8")
    lines: list[str] = []
    answers = ("Custom endpoint", "http://127.0.0.1:8000/v1/", "", "Keep")
    assert run(clean, *answers, lines=lines) == 0
    assert saved(clean)["model"] == {
        "default": "qwen",
        "provider": "custom",
        "base_url": "http://127.0.0.1:8000/v1",
    }
    assert "The endpoint offers one model: qwen" in lines
    assert "OPENAI_BASE_URL" in env_file.read_text(encoding="utf-8")


def test_local_server_with_default_url(clean: Scope, mocked: respx.MockRouter) -> None:
    mocked.get("http://127.0.0.1:11434/v1/models").respond(
        200, json={"data": [{"id": "llama3"}, {"id": "qwen3"}]}
    )
    assert run(clean, "Local server", "Ollama", "", "qwen3", "Keep") == 0
    assert saved(clean)["model"] == {"default": "qwen3", "provider": "ollama"}


def test_listing_failure_falls_back_to_typing(clean: Scope, mocked: respx.MockRouter) -> None:
    mocked.get(DEEPSEEK_MODELS).respond(503)
    assert run(clean, "DeepSeek", FAKE_KEY, "typed-model", "Keep") == 0
    assert saved(clean)["model"] == {"default": "typed-model", "provider": "deepseek"}


def test_reasoning_only(clean: Scope) -> None:
    config.save(clean, {"model": {"provider": "deepseek", "default": "deepseek-chat"}})
    assert run(clean, "Reasoning effort for the current model", "low") == 0
    raw = saved(clean)
    assert raw["agent"] == {"reasoning_effort": "low"}
    assert raw["model"] == {"provider": "deepseek", "default": "deepseek-chat"}


def test_non_tty_exits_1(clean: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    driver = ScriptedDriver([])
    assert run_model(clean, Interactive(tty=False, driver=driver)) == 1
    assert "needs a terminal" in capsys.readouterr().err
    assert driver.asked == []


def test_refresh_clears_the_cache(clean: Scope, mocked: respx.MockRouter) -> None:
    assert run(clean, "OpenRouter", "Paste an API key", FAKE_KEY, "a/one", "Keep") == 0
    first = mocked.calls.call_count
    assert run(clean, "OpenRouter", "Keep it", "a/one", "Keep") == 0
    assert mocked.calls.call_count == first
    io = scripted("OpenRouter", "Keep it", "a/one", "Keep")
    assert run_model(clean, io, deps=PickerDeps(refresh=True)) == 0
    assert mocked.calls.call_count == first + 1


def test_selection_repr_hides_the_key() -> None:
    chosen = Selection(provider="x", model="m", key_name="K", key_value=FAKE_KEY)
    assert FAKE_KEY not in repr(chosen)
    assert chosen.entry() == {"provider": "x", "model": "m"}


def test_apply_keeps_unrelated_settings(clean: Scope) -> None:
    path = config.config_path(clean)
    config.save(clean, {"terminal": {"timeout": 99}})
    path.write_text("# mine\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    apply(clean, Selection(provider="deepseek", model="deepseek-chat"))
    text = path.read_text(encoding="utf-8")
    assert "# mine" in text and "timeout: 99" in text


# -- active provider ----------------------------------------------------------------------------


def _active(scope: Scope) -> tuple[str | None, list[str]]:
    loader.reset()
    fresh = scope.with_config(config.load(scope))
    return active_provider(fresh, Registry(fresh))


def test_active_provider_table(clean: Scope) -> None:
    assert _active(clean) == (None, [])
    keyed = clean.with_env(clean.env.with_overlay({"OPENROUTER_API_KEY": FAKE_KEY}))
    assert _active(keyed) == ("openrouter", [])
    env_named = keyed.with_env(keyed.env.with_overlay({"INDUS_INFERENCE_PROVIDER": "xai"}))
    assert _active(env_named) == ("xai", [])
    config.save(clean, {"model": {"provider": "deepseek"}})
    assert _active(env_named) == ("deepseek", [])
    config.save(clean, {"model": {"provider": "nope"}})
    name, notes = _active(keyed)
    assert name == "openrouter" and "'nope'" in notes[0]
    config.save(
        clean,
        {
            "providers": {"mybox": {"base_url": "http://10.0.0.5:8000/v1"}},
            "model": {"provider": "custom", "base_url": "http://10.0.0.5:8000/v1"},
        },
    )
    assert _active(clean) == ("mybox", [])


# -- cache ------------------------------------------------------------------------------------


def test_cache_ttls(clean: Scope, fake_clock: FakeClock) -> None:
    calls: list[int] = []
    answers: list[list[str]] = [["m1"], ["m2"], [], ["m3"]]

    def fetch() -> list[str]:
        calls.append(1)
        return answers.pop(0)

    jobs: list[Callable[[], None]] = []
    key, fp = model_cache.row_key("p", "https://h/v1/"), model_cache.fingerprint(FAKE_KEY)

    def get(**kw: object) -> list[str]:
        return model_cache.get_models(clean, key, fp, fetch, revalidate=jobs.append, **kw)  # type: ignore[arg-type]

    assert get() == ["m1"] and len(calls) == 1
    fake_clock.advance(model_cache.LIVE_TTL_S - 1)
    assert get() == ["m1"] and len(calls) == 1
    fake_clock.advance(2)
    assert model_cache.lookup(clean, key, fp).state == "stale"
    assert get() == ["m1"] and len(jobs) == 1 and len(calls) == 1
    jobs.pop()()
    assert model_cache.lookup(clean, key, fp) == model_cache.Lookup("fresh", ("m2",))
    # Another key never sees this list.
    assert model_cache.lookup(clean, key, model_cache.fingerprint(NEW_KEY)).state == "miss"
    # Past the stale window: a live fetch; it fails, so the old live list is served, unchanged.
    fake_clock.advance(model_cache.STALE_MAX_S + 1)
    assert get() == ["m2"] and len(calls) == 3
    assert model_cache.lookup(clean, key, fp).state == "miss"
    assert get() == ["m3"]
    assert model_cache.clear(clean) and not model_cache.clear(clean)


def test_fallback_rows_and_negative_probe(clean: Scope, fake_clock: FakeClock) -> None:
    calls: list[int] = []

    def failing() -> list[str]:
        calls.append(1)
        return []

    key = model_cache.row_key("q", "https://q/v1")
    got = model_cache.get_models(clean, key, "", failing, curated=["c1"])
    assert got == ["c1"] and model_cache.lookup(clean, key, "").state == "fallback"
    fake_clock.advance(model_cache.FALLBACK_TTL_S - 1)
    assert model_cache.get_models(clean, key, "", failing, curated=["c1"]) == ["c1"]
    assert len(calls) == 1
    fake_clock.advance(2)
    assert model_cache.lookup(clean, key, "").state == "miss"
    dead = model_cache.row_key("dead", "http://127.0.0.1:1/v1")
    assert model_cache.get_models(clean, dead, "", failing) == []
    assert model_cache.get_models(clean, dead, "", failing) == []
    assert len(calls) == 2
    fake_clock.advance(model_cache.NEGATIVE_TTL_S + 1)
    assert model_cache.get_models(clean, dead, "", failing) == []
    assert len(calls) == 3


def test_broken_cache_file_is_ignored(clean: Scope) -> None:
    path = model_cache.cache_path(clean)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert model_cache.lookup(clean, "k", "").state == "miss"
    assert model_cache.get_models(clean, "k", "", lambda: ["x"]) == ["x"]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == model_cache.VERSION


def test_fetch_errors_are_swallowed(clean: Scope) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    deps = PickerDeps(client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    io = scripted("DeepSeek", FAKE_KEY, "typed", "Keep")
    assert run_model(clean, io, deps=deps) == 0
    assert saved(clean)["model"] == {"default": "typed", "provider": "deepseek"}
