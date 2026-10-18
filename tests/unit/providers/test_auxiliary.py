"""The auxiliary model client: routes, fall-through, usage tags, windows, titles (P08-17)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from indus.foundation.scope import Scope
from indus.providers import auxiliary, context_length
from indus.providers.auxiliary import (
    AuxClient,
    AuxTimeout,
    AuxUnavailable,
    auto_title,
    clean_title,
)
from indus.providers.context_length import ContextWindow
from indus.providers.retry import RetryFailed
from indus.providers.route import RuntimeRoute
from indus.state import sessions, titles
from indus.state.registry import open_unshared
from tests.fakes.clock import FakeClock
from tests.fakes.turn import drive
from tests.unit.providers._support import FAKE_KEY, make_route
from tests.unit.providers.conftest import ScopeFactory

MAIN = make_route(base_url="https://main.example/v1", model="main-model", provider="custom")
KEYS = {"OPENAI_API_KEY": FAKE_KEY, "DEEPSEEK_API_KEY": FAKE_KEY, "XAI_API_KEY": FAKE_KEY}
TITLE_CONFIG = {
    "auxiliary": {
        "title": {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "fallback": [
                {"provider": "xai", "model": "grok-small"},
                {"provider": "mistral"},  # no model: ignored
                {"provider": "deepseek", "model": "deepseek-flash"},  # duplicate
            ],
        }
    },
    "fallback_providers": [
        {"provider": "nowhere", "model": "m"},
        {"provider": "xai", "model": "grok-small"},
    ],
}


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def reply(text: str, *, prompt: int = 11, completion: int = 4) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
    )


class Hosts:
    """A mock HTTP layer: one handler per host, every request kept."""

    def __init__(self, **by_host: Callable[[httpx.Request], Any]) -> None:
        self.by_host = by_host
        self.seen: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        handler = self.by_host.get(request.url.host)
        if handler is None:
            return httpx.Response(404, json={"error": {"message": "no such host"}})
        result = handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def factory(self, route: RuntimeRoute) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.seen]


def client(scope: Scope, hosts: Hosts, **kw: Any) -> AuxClient:
    return AuxClient(scope, MAIN, client_factory=hosts.factory, **kw)


def test_resolution_order_table(make_scope: ScopeFactory) -> None:
    scope = make_scope(config=TITLE_CONFIG, env=KEYS)
    aux = AuxClient(scope, MAIN)
    title = [(c.origin, c.provider, c.model) for c in aux.candidates("title")]
    assert title == [
        ("pin", "deepseek", "deepseek-flash"),
        ("task-fallback", "xai", "grok-small"),
        ("main-fallback", "nowhere", "m"),
    ]
    other = [(c.origin, c.provider, c.model) for c in aux.candidates("compression")]
    assert other == [
        ("main", "auto", ""),
        ("main-fallback", "nowhere", "m"),
        ("main-fallback", "xai", "grok-small"),
    ]
    assert aux.route_for(aux.candidates("compression")[0]) is MAIN
    pinned = aux.route_for(aux.candidates("title")[0])
    assert (pinned.provider, pinned.model, pinned.base_url) == (
        "deepseek",
        "deepseek-flash",
        "https://api.deepseek.com/v1",
    )


def test_model_only_pin_swaps_the_model_on_the_main_route(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"auxiliary": {"title": {"model": "small-one"}}})
    aux = AuxClient(scope, MAIN)
    route = aux.route_for(aux.candidates("title")[0])
    assert (route.provider, route.base_url, route.model) == ("custom", MAIN.base_url, "small-one")


def test_auto_uses_the_main_route(make_scope: ScopeFactory) -> None:
    hosts = Hosts(**{"main.example": lambda r: reply("summary text")})
    aux = client(make_scope(), hosts)
    result = run(aux.call("compression", [{"role": "user", "content": "x"}]))
    assert (result.text, result.origin, result.route) == ("summary text", "main", MAIN)
    body = json.loads(hosts.seen[0].content)
    assert body["model"] == "main-model" and not body.get("stream")


def test_402_falls_through_to_next_candidate(make_scope: ScopeFactory) -> None:
    hosts = Hosts(
        **{
            "api.deepseek.com": lambda r: httpx.Response(
                402, json={"error": {"message": "Insufficient balance on this account"}}
            ),
            "api.x.ai": lambda r: reply("A short title"),
        }
    )
    aux = client(make_scope(config=TITLE_CONFIG, env=KEYS), hosts)
    result = run(aux.call("title", [{"role": "user", "content": "x"}]))
    assert result.text == "A short title" and result.origin == "task-fallback"
    assert hosts.hosts() == ["api.deepseek.com", "api.x.ai"]


def test_other_failures_belong_to_the_caller(make_scope: ScopeFactory) -> None:
    hosts = Hosts(
        **{
            "api.deepseek.com": lambda r: httpx.Response(
                401, json={"error": {"message": "bad key"}}
            )
        }
    )
    aux = client(make_scope(config=TITLE_CONFIG, env=KEYS), hosts)
    with pytest.raises(RetryFailed):
        run(aux.call("title", [{"role": "user", "content": "x"}]))
    assert hosts.hosts() == ["api.deepseek.com"]


def test_unusable_candidates_are_skipped_then_unavailable(make_scope: ScopeFactory) -> None:
    # no keys at all: the pin and the task fallback cannot be built; the main chain's unknown
    # provider neither; every reason is reported in order
    hosts = Hosts()
    aux = client(make_scope(config=TITLE_CONFIG), hosts)
    with pytest.raises(AuxUnavailable) as caught:
        run(aux.call("title", [{"role": "user", "content": "x"}]))
    assert [r.split()[0] for r in caught.value.reasons] == ["pin", "task-fallback", "main-fallback"]
    assert hosts.seen == []


def test_usage_recorded_with_task_tag(make_scope: ScopeFactory) -> None:
    seen: list[tuple[str, str, int, float]] = []
    phases: list[str] = []

    def broken_observer(phase: str, task: str, info: Any) -> None:
        raise RuntimeError("observer bug")

    hosts = Hosts(**{"main.example": lambda r: reply("ok", prompt=30, completion=2)})
    aux = client(
        make_scope(),
        hosts,
        record=lambda task, route, usage, latency: seen.append(
            (task, route.provider, usage.prompt_tokens if usage else -1, latency)
        ),
        observers=[lambda phase, task, info: phases.append(f"{phase}:{task}"), broken_observer],
    )
    run(aux.call("compression", [{"role": "user", "content": "x"}]))
    assert seen == [("compression", "custom", 30, 0.0)]
    assert phases == ["before:compression", "after:compression"]


def test_timeout_on_the_fake_clock(make_scope: ScopeFactory, fake_clock: FakeClock) -> None:
    async def never(request: httpx.Request) -> httpx.Response:
        await fake_clock.sleep(10_000)
        return reply("late")

    hosts = Hosts(**{"main.example": never})
    scope = make_scope(config={"auxiliary": {"title": {"timeout_s": 5}}})
    aux = client(scope, hosts, clock=fake_clock)
    assert aux.timeout_for("title") == 5 and aux.timeout_for("approval") == 30

    async def main() -> None:
        task = asyncio.ensure_future(aux.call("title", [{"role": "user", "content": "x"}]))
        await drive(task, fake_clock, 1.0)
        with pytest.raises(AuxTimeout):
            task.result()
        assert fake_clock.monotonic() < 100

    run(main())


def test_small_window_warning_once(
    make_scope: ScopeFactory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def window(
        route: RuntimeRoute, *, scope: Scope, list_models: Any = None
    ) -> ContextWindow:
        return ContextWindow(32_000 if route.model == "tiny" else 1_000_000, "test")

    monkeypatch.setattr(context_length, "find_window", window)
    aux = AuxClient(make_scope(config={"auxiliary": {"compression": {"model": "tiny"}}}), MAIN)
    with caplog.at_level(logging.WARNING, logger="indus.providers.auxiliary"):
        assert run(aux.window_for("compression", 200_000)) == 32_000
        assert run(aux.window_for("compression", 200_000)) == 32_000
    assert caplog.text.count("smaller than the main model") == 1
    # ``auto`` (no pin) is the main route: the main window, without a lookup
    assert run(aux.window_for("title", 200_000)) == 200_000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Fixing the flaky login test."', "Fixing the flaky login test"),
        ("Title: Budget review for Q3", "Budget review for Q3"),
        ("**Deploy plan**\nextra words", "Deploy plan"),
        ("one two three four five six seven eight nine", "one two three four five six seven"),
        ("  \n  ", None),
    ],
)
def test_clean_title(raw: str, expected: str | None) -> None:
    assert clean_title(raw) == expected


def test_auto_title_writes_llm_title_and_keeps_user_titles(make_scope: ScopeFactory) -> None:
    scope = make_scope()
    store = open_unshared(scope)
    try:
        sessions.create(store, "20261018_000000_aaaaaa", source="cli")
        sessions.create(store, "20261018_000000_bbbbbb", source="cli")
        sessions.create(store, "20261018_000000_cccccc", source="cli")
        hosts = Hosts(**{"main.example": lambda r: reply('"Fix the flaky login test."')})
        aux = client(scope, hosts)
        got = run(auto_title(aux, store, "20261018_000000_aaaaaa", "the login test flakes", "ok"))
        assert got == "Fix the flaky login test"
        assert titles.get_title(store, "20261018_000000_aaaaaa") == got
        # the same title elsewhere: the next number is used
        again = run(auto_title(aux, store, "20261018_000000_bbbbbb", "same", "ok"))
        assert again == "Fix the flaky login test #2"
        titles.set_title(store, "20261018_000000_cccccc", "Mine", source="user")
        assert run(auto_title(aux, store, "20261018_000000_cccccc", "x", "y")) is None
        assert titles.get_title(store, "20261018_000000_cccccc") == "Mine"
        body = json.loads(hosts.seen[0].content)
        assert body["messages"][0]["content"] == auxiliary.TITLE_PROMPT
        assert body["max_tokens"] == 32 or body.get("max_completion_tokens") == 32
    finally:
        store.close()
