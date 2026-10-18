"""web_search and web_extract over fake providers, a fake clock and a fake resolver (P05-23)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from indus.foundation.scope import Scope, make_scope
from indus.safety import url_policy
from indus.tools.builtin.web import EXTRACT_SCHEMA, SEARCH_SCHEMA, WebTools
from indus.tools.builtin.web.extract import (
    DEFAULT_CHAR_LIMIT,
    MAX_STORED_CHARS,
    binary_kind,
    clamp_char_limit,
    cut_page,
    replace_inline_images,
    run_extract,
    store_full_text,
)
from indus.tools.builtin.web.providers import (
    BraveProvider,
    DirectProvider,
    DuckDuckGoProvider,
    NoProvider,
    Page,
    ProviderFailed,
    ProviderRegistry,
    SearxngProvider,
    choose,
    default_registry,
    html_to_text,
)
from indus.tools.builtin.web.search import SearchMemo, bucket_for, clamp_limit, fold_query
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.resolver import FakeResolver
from tests.fakes.web_provider import FakeWebProvider, failing, hits, many_hits

PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def _fresh_blocklist() -> Iterator[None]:
    url_policy.clear_blocklist_cache()
    yield
    url_policy.clear_blocklist_cache()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 3, 1, tzinfo=UTC))


def make(
    tmp_path: Path,
    clock: FakeClock,
    config: Mapping[str, Any] | None = None,
    *,
    name: str = "home",
    env: Mapping[str, str] | None = None,
) -> Scope:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return make_scope(home, env=env or {}, clock=clock).with_config(DictConfig(config or {}))


def tools_with(*providers: FakeWebProvider, resolver: FakeResolver | None = None) -> WebTools:
    return WebTools(ProviderRegistry(providers), resolver=resolver or FakeResolver())


def search(tools: WebTools, scope: Scope, **args: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(tools.search(args, scope=scope)))


def extract(tools: WebTools, scope: Scope, **args: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(tools.extract(args, scope=scope)))


# -- limits and the memo ---------------------------------------------------------------------


def test_limit_clamped_and_bucketed(tmp_path: Path, clock: FakeClock) -> None:
    table = [(None, 5), ("x", 5), (True, 5), (0, 1), (-4, 1), (1, 1), ("7", 7), (100, 100)]
    table += [(101, 100), (5.9, 5)]
    for raw, expected in table:
        assert clamp_limit(raw) == expected, raw
    assert [bucket_for(n) for n in (1, 10, 11, 20, 21, 50, 51, 100)] == [
        10,
        10,
        20,
        20,
        50,
        50,
        100,
        100,
    ]
    provider = FakeWebProvider().script(many_hits(30))
    tools = tools_with(provider)
    scope = make(tmp_path, clock)
    three = search(tools, scope, query="q", limit=3)
    seven = search(tools, scope, query="q", limit=7)
    assert provider.search_calls == [("q", 10)]
    assert [r["position"] for r in three["results"]] == [1, 2, 3]
    assert len(seven["results"]) == 7 and seven["provider"] == "fake"
    search(tools, scope, query="q", limit=15)
    assert provider.search_calls[-1] == ("q", 20)


def test_blank_query_is_an_error(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider()
    tools = tools_with(provider)
    for query in ["", "   ", None, 7]:
        assert "error" in search(tools, make(tmp_path, clock), query=query)
    assert provider.search_calls == []


def test_memo_hit_and_expiry(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider().script(hits("https://a.example/"))
    tools = tools_with(provider)
    scope = make(tmp_path, clock)
    first = search(tools, scope, query="Python  Asyncio")
    clock.advance(20 * 60 - 1)
    again = search(tools, scope, query="python asyncio")
    assert again == {**first, "query": "python asyncio"}
    assert len(provider.search_calls) == 1
    clock.advance(2)
    search(tools, scope, query="python asyncio")
    assert len(provider.search_calls) == 2
    assert fold_query("  A\tB  c ") == "a b c"


def test_memo_never_stores_failures(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider().script(failing("quota"), hits("https://a.example/"))
    tools = tools_with(provider)
    scope = make(tmp_path, clock)
    failed = search(tools, scope, query="q")
    assert "quota" in failed["error"]
    assert search(tools, scope, query="q")["results"][0]["url"] == "https://a.example/"
    assert len(provider.search_calls) == 2


def test_memo_is_bounded_and_keyed_by_profile(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider().script(hits("https://a.example/"))
    tools = WebTools(ProviderRegistry([provider]), memo=SearchMemo(max_entries=3))
    scope = make(tmp_path, clock)
    for query in ["one", "two", "three", "four"]:
        search(tools, scope, query=query)
    assert len(tools.memo) == 3
    search(tools, scope, query="four")
    search(tools, scope, query="one")  # evicted first
    assert [q for q, _ in provider.search_calls] == ["one", "two", "three", "four", "one"]
    other = make(tmp_path, clock, name="other-profile")
    search(tools, other, query="four")
    assert len(provider.search_calls) == 6


def test_memo_keys_include_the_provider(tmp_path: Path, clock: FakeClock) -> None:
    first = FakeWebProvider("first").script(hits("https://1.example/"))
    second = FakeWebProvider("second").script(hits("https://2.example/"))
    tools = tools_with(first, second)
    a = search(tools, make(tmp_path, clock), query="q")
    b = search(tools, make(tmp_path, clock, {"web": {"search_backend": "second"}}), query="q")
    assert (a["provider"], b["provider"]) == ("first", "second")
    assert len(first.search_calls) == len(second.search_calls) == 1


def test_single_flight_shares_one_call(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider().script(hits("https://a.example/"))
    tools = tools_with(provider)
    scope = make(tmp_path, clock)

    async def go() -> list[str]:
        provider.gate = asyncio.Event()
        tasks = [
            asyncio.create_task(tools.search({"query": "same"}, scope=scope)) for _ in range(3)
        ]
        while provider.entered < 1:
            await checkpoint()
        for _ in range(5):
            await checkpoint()
        provider.gate.set()
        return list(await asyncio.gather(*tasks))

    results = asyncio.run(go())
    assert len(provider.search_calls) == 1
    assert len(set(results)) == 1


def test_single_flight_failure_reaches_every_waiter(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider().script(failing("boom"))
    tools = tools_with(provider)
    scope = make(tmp_path, clock)

    async def go() -> list[str]:
        provider.gate = asyncio.Event()
        tasks = [asyncio.create_task(tools.search({"query": "q"}, scope=scope)) for _ in range(2)]
        while provider.entered < 1:
            await checkpoint()
        await checkpoint()
        provider.gate.set()
        return list(await asyncio.gather(*tasks))

    for text in asyncio.run(go()):
        assert "boom" in json.loads(text)["error"]
    assert len(provider.search_calls) == 1


def test_single_flight_survives_a_cancelled_leader(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider().script(hits("https://a.example/"))
    tools = tools_with(provider)
    scope = make(tmp_path, clock)

    async def go() -> str:
        provider.gate = asyncio.Event()
        leader = asyncio.create_task(tools.search({"query": "q"}, scope=scope))
        while provider.entered < 1:
            await checkpoint()
        follower = asyncio.create_task(tools.search({"query": "q"}, scope=scope))
        await checkpoint()
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        while provider.entered < 2:
            await checkpoint()
        provider.gate.set()
        return await follower

    body = json.loads(asyncio.run(go()))
    assert body["results"][0]["url"] == "https://a.example/"
    assert len(provider.search_calls) == 2
    assert len(tools.memo) == 1


# -- choosing and rescue -----------------------------------------------------------------------


def test_explicit_unknown_or_unusable_provider_is_an_error(
    tmp_path: Path, clock: FakeClock
) -> None:
    keyed = FakeWebProvider("keyed", missing="KEYED_API_KEY is not set")
    spare = FakeWebProvider("spare", keyless=True).script(hits("https://s.example/"))
    tools = tools_with(keyed, spare)
    unknown = make(tmp_path, clock, {"web": {"search_backend": "nope"}})
    assert "'nope'" in search(tools, unknown, query="q")["error"]
    unusable = make(tmp_path, clock, {"web": {"backend": "keyed"}})
    error = search(tools, unusable, query="q")["error"]
    assert "keyed" in error and "KEYED_API_KEY" in error
    assert spare.search_calls == []
    assert search(tools, make(tmp_path, clock), query="q")["provider"] == "spare"


def test_explicit_search_only_provider_for_extract(tmp_path: Path, clock: FakeClock) -> None:
    searcher = FakeWebProvider("searcher", extract=False)
    reader = FakeWebProvider("reader", search=False).serve({"https://a.example/": "text"})
    registry = ProviderRegistry([searcher, reader])
    strict = make(tmp_path, clock, {"web": {"extract_backend": "searcher"}})
    with pytest.raises(NoProvider) as caught:
        choose(registry, strict, "extract")
    assert caught.value.explicit and "reader" in str(caught.value)
    shared = make(tmp_path, clock, {"web": {"backend": "searcher"}})
    assert choose(registry, shared, "search").name == "searcher"
    assert choose(registry, shared, "extract").name == "reader"


def test_nothing_usable_names_what_to_set(tmp_path: Path, clock: FakeClock) -> None:
    scope = make(tmp_path, clock)
    registry = ProviderRegistry([BraveProvider(), SearxngProvider()])
    with pytest.raises(NoProvider) as caught:
        choose(registry, scope, "search")
    assert not caught.value.explicit
    assert "BRAVE_SEARCH_API_KEY" in str(caught.value) and "searxng_url" in str(caught.value)
    tools = WebTools(registry)
    assert tools.search_ready(scope) is False
    assert tools.search_ready(make(tmp_path, clock, {"web": {"backend": "brave"}})) is True


def test_rescue_serves_once_and_is_not_cached(tmp_path: Path, clock: FakeClock) -> None:
    keyed = FakeWebProvider("keyed").script(failing("429 too many"))
    spare = FakeWebProvider("spare", keyless=True).script(hits("https://s.example/"))
    tools = tools_with(keyed, spare)
    scope = make(tmp_path, clock)
    first = search(tools, scope, query="q")
    assert first["provider"] == "spare" and first["rescued_from"] == "keyed"
    assert "429" in first["note"] and "keyed again" in first["note"]
    search(tools, scope, query="q")
    assert len(keyed.search_calls) == 2 and len(spare.search_calls) == 2
    assert len(tools.memo) == 0


def test_rescue_after_an_unexpected_exception(tmp_path: Path, clock: FakeClock) -> None:
    keyed = FakeWebProvider("keyed").script(RuntimeError("socket closed"))
    spare = FakeWebProvider("spare", keyless=True).script(hits("https://s.example/"))
    body = search(tools_with(keyed, spare), make(tmp_path, clock), query="q")
    assert body["rescued_from"] == "keyed" and "socket closed" in body["note"]


def test_no_rescue_when_off_or_keyless(tmp_path: Path, clock: FakeClock) -> None:
    keyed = FakeWebProvider("keyed").script(failing("down"))
    spare = FakeWebProvider("spare", keyless=True).script(failing("also down"))
    tools = tools_with(keyed, spare)
    off = make(tmp_path, clock, {"web": {"keyless_rescue": False}})
    assert "down" in search(tools, off, query="q")["error"]
    assert spare.search_calls == []
    keyless_only = make(tmp_path, clock, {"web": {"search_backend": "spare"}})
    error = search(tools, keyless_only, query="other")["error"]
    assert "also down" in error and len(spare.search_calls) == 1


def test_failed_rescue_keeps_the_original_error_first(tmp_path: Path, clock: FakeClock) -> None:
    keyed = FakeWebProvider("keyed").script(failing("invalid key"))
    spare = FakeWebProvider("spare", keyless=True).script(failing("rate limited"))
    error = search(tools_with(keyed, spare), make(tmp_path, clock), query="q")["error"]
    assert error.index("invalid key") < error.index("rate limited")


# -- extract -------------------------------------------------------------------------------------


def resolver_for(**hosts: str) -> FakeResolver:
    return FakeResolver({host.replace("_", "."): [address] for host, address in hosts.items()})


def test_extract_keeps_order_and_blocks_per_url(tmp_path: Path, clock: FakeClock) -> None:
    resolver = resolver_for(a_example=PUBLIC, b_example="10.0.0.2", c_example=PUBLIC)
    provider = FakeWebProvider(search=False).serve(
        {"https://a.example/": "page A", "https://c.example/": "page C"}
    )
    tools = tools_with(provider, resolver=resolver)
    body = extract(
        tools,
        make(tmp_path, clock),
        urls=["https://a.example/", "https://b.example/", "https://c.example/"],
    )
    urls = [r["url"] for r in body["results"]]
    assert urls == ["https://a.example/", "https://b.example/", "https://c.example/"]
    assert body["results"][0]["content"] == "page A" and body["results"][0]["error"] is None
    assert body["results"][1]["error"].startswith("Blocked:")
    assert "private" in body["results"][1]["error"]
    assert provider.extract_calls == [
        (["https://a.example/", "https://c.example/"], DEFAULT_CHAR_LIMIT)
    ]


def test_extract_secret_and_metadata_urls_never_reach_the_provider(
    tmp_path: Path, clock: FakeClock
) -> None:
    provider = FakeWebProvider(search=False)
    tools = tools_with(provider, resolver=resolver_for(a_example=PUBLIC))
    body = extract(
        tools,
        make(tmp_path, clock),
        urls=["https://a.example/cb?access_token=abc", "http://169.254.169.254/latest/"],
    )
    assert "error" in body and "inaccessible" in body["error"]
    assert all(r["error"].startswith("Blocked:") for r in body["results"])
    assert provider.extract_calls == []


def test_extract_items_from_search_hits_and_junk(tmp_path: Path, clock: FakeClock) -> None:
    resolver = resolver_for(a_example=PUBLIC, b_example=PUBLIC)
    provider = FakeWebProvider(search=False).serve(
        {"https://a.example/": "A", "https://b.example/K%C3%B6ln": "B"}
    )
    tools = tools_with(provider, resolver=resolver)
    body = extract(
        tools,
        make(tmp_path, clock),
        urls=[
            {"url": "https://a.example/", "title": "x"},
            42,
            {"href": " https://b.example/Köln "},
        ],
    )
    contents = [r["content"] for r in body["results"]]
    assert contents == ["A", "", "B"]
    assert "item 1" in body["results"][1]["error"]


def test_extract_takes_five_urls_and_says_so(tmp_path: Path, clock: FakeClock) -> None:
    resolver = FakeResolver({f"h{n}.example": [PUBLIC] for n in range(7)})
    urls = [f"https://h{n}.example/" for n in range(7)]
    provider = FakeWebProvider(search=False).serve({u: "x" for u in urls})
    body = extract(tools_with(provider, resolver=resolver), make(tmp_path, clock), urls=urls)
    assert len(body["results"]) == 5 and "first 5" in body["note"]
    assert provider.extract_calls[0][0] == urls[:5]


def test_extract_bad_urls_argument(tmp_path: Path, clock: FakeClock) -> None:
    tools = tools_with(FakeWebProvider(search=False))
    for raw in [None, [], {}, 3]:
        assert "error" in extract(tools, make(tmp_path, clock), urls=raw)


def test_extract_accepts_a_single_url_text(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider(search=False).serve({"https://a.example/": "A"})
    tools = tools_with(provider, resolver=resolver_for(a_example=PUBLIC))
    assert (
        extract(tools, make(tmp_path, clock), urls="https://a.example/")["results"][0]["content"]
        == "A"
    )
    listed = extract(tools, make(tmp_path, clock), urls='["https://a.example/"]')
    assert listed["results"][0]["content"] == "A"


def test_extract_cuts_long_pages_with_a_footer_and_stores_them(
    tmp_path: Path, clock: FakeClock
) -> None:
    lines = [f"line {n:05d} " + "word " * 10 for n in range(1000)]
    page = "\n".join(lines)
    provider = FakeWebProvider(search=False).serve({"https://long.example/doc": page})
    scope = make(tmp_path, clock)
    tools = tools_with(provider, resolver=resolver_for(long_example=PUBLIC))
    body = extract(tools, scope, urls=["https://long.example/doc"], char_limit=4000)
    text = body["results"][0]["content"]
    assert text.startswith("line 00000")
    assert "line 00999" in text and "line 00500" not in text
    assert "==== page cut ====" in text
    saved = Path(text.split("Full text saved at: ", 1)[1].split("\n", 1)[0])
    assert saved.parent == scope.home / "cache" / "web"
    assert saved.read_text() == page
    shown_head = text.split("\n\n[... the middle", 1)[0]
    offset = int(text.split("offset=", 1)[1].split(" ", 1)[0])
    assert offset == shown_head.count("\n") + 2
    assert page.splitlines()[offset - 1] not in shown_head


def test_extract_short_page_is_whole(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider(search=False).serve({"https://a.example/": "short page"})
    scope = make(tmp_path, clock)
    body = extract(
        tools_with(provider, resolver=resolver_for(a_example=PUBLIC)),
        scope,
        urls=["https://a.example/"],
    )
    assert body["results"][0]["content"] == "short page"
    assert not (scope.home / "cache" / "web").exists()


def test_char_limit_is_clamped() -> None:
    table = [(None, 15_000), ("abc", 15_000), (True, 15_000), (10, 2_000), ("3000", 3_000)]
    table += [(10**9, 500_000)]
    for raw, expected in table:
        assert clamp_char_limit(raw) == expected, raw


def test_cut_page_without_storage_and_without_newlines() -> None:
    text = "x" * 10_000
    cut = cut_page(text, 2_000, None)
    assert "could not be saved" in cut
    assert cut.startswith("x" * 1_500) and "read_file" not in cut
    assert cut_page("abc", 2_000, None) == "abc"


def test_stored_copy_is_bounded(tmp_path: Path) -> None:
    text = "y" * (MAX_STORED_CHARS + 10)
    saved = store_full_text(tmp_path, "https://big.example/x", text)
    assert saved is not None
    stored = saved.read_text()
    assert stored.startswith("y" * 1000) and "stored copy stops at 2,000,000" in stored
    assert len(stored) < MAX_STORED_CHARS + 200
    blocked = tmp_path / "file"
    blocked.write_text("")
    assert store_full_text(blocked, "https://a.example/", "z") is None


def test_inline_images_become_placeholders() -> None:
    blob = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB" * 50
    text = (
        f"before ![a chart](data:image/png;base64,{blob}) mid "
        f"![](data:image/jpeg;base64,{blob}) and (data:image/gif;base64,{blob}) "
        f"bare data:image/webp;base64,{blob} end ![kept](https://img.example/x.png)"
    )
    out = replace_inline_images(text)
    assert out == (
        "before [image: a chart] mid [image] and [image] bare [image] end "
        "![kept](https://img.example/x.png)"
    )


def test_binary_payloads_are_refused(tmp_path: Path, clock: FakeClock) -> None:
    assert binary_kind("PK\x03\x04rest") == "ZIP archive"
    assert binary_kind("SQLite format 3\x00...") == "SQLite database"
    assert binary_kind("\x89PNG\r\n\x1a\n....") == "PNG image"
    assert binary_kind("PKZIP is a file format") is None
    assert binary_kind("# Heading\ntext") is None
    provider = FakeWebProvider(search=False).serve(
        {"https://a.example/x.zip": "PK\x03\x04" + "\x01" * 50}
    )
    body = extract(
        tools_with(provider, resolver=resolver_for(a_example=PUBLIC)),
        make(tmp_path, clock),
        urls=["https://a.example/x.zip"],
    )
    assert "ZIP archive" in body["results"][0]["error"]
    assert body["results"][0]["content"] == ""


def test_extract_provider_timeout_gives_per_url_errors(tmp_path: Path, clock: FakeClock) -> None:
    provider = FakeWebProvider(search=False).serve({"https://a.example/": "never"})
    tools = tools_with(provider, resolver=resolver_for(a_example=PUBLIC, b_example=PUBLIC))
    scope = make(tmp_path, clock)

    async def go() -> dict[str, Any]:
        provider.gate = asyncio.Event()  # never set: the provider hangs
        task = asyncio.create_task(
            run_extract(
                tools.providers,
                {"urls": ["https://a.example/", "https://b.example/"]},
                scope,
                resolver=tools.resolver,
            )
        )
        while provider.entered < 1:
            await checkpoint()
        await clock.advance_and_settle(119)
        assert not task.done()
        await clock.advance_and_settle(2)
        return await task

    body = asyncio.run(go())
    assert all("no answer within 120 s" in r["error"] for r in body["results"])
    assert "inaccessible" in body["error"]


def test_extract_provider_failure_and_partial_answers(tmp_path: Path, clock: FakeClock) -> None:
    resolver = resolver_for(a_example=PUBLIC, b_example=PUBLIC, c_example=PUBLIC)
    urls = ["https://a.example/", "https://b.example/", "https://c.example/"]
    broken = FakeWebProvider(search=False)
    broken.extract_error = ProviderFailed("vendor 500")
    body = extract(tools_with(broken, resolver=resolver), make(tmp_path, clock), urls=urls)
    assert all("vendor 500" in r["error"] for r in body["results"])

    shuffled = FakeWebProvider(search=False)
    shuffled.extract_reply = [
        Page("https://c.example/", content="C"),
        Page("https://a.example/", content="A"),
    ]
    body = extract(tools_with(shuffled, resolver=resolver), make(tmp_path, clock), urls=urls)
    assert [r["content"] for r in body["results"]] == ["A", "", "C"]
    assert "nothing for this URL" in body["results"][1]["error"]
    assert "error" not in body

    redirected = FakeWebProvider(search=False)
    redirected.extract_reply = [Page("https://www.a.example/home", content="A")]
    body = extract(tools_with(redirected, resolver=resolver), make(tmp_path, clock), urls=urls[:1])
    assert body["results"][0] == {
        "url": "https://a.example/",
        "title": "",
        "content": "A",
        "error": None,
    }


def test_extract_without_a_provider(tmp_path: Path, clock: FakeClock) -> None:
    tools = tools_with(FakeWebProvider(extract=False), resolver=resolver_for(a_example=PUBLIC))
    assert (
        "can extract pages here (none is registered)"
        in extract(tools, make(tmp_path, clock), urls=["https://a.example/"])["error"]
    )


# -- built-in providers over mocked HTTP ---------------------------------------------------------


@respx.mock
def test_searxng_adapter(tmp_path: Path, clock: FakeClock) -> None:
    route = respx.get("http://searx.local:8080/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://low.example/", "title": "Low", "content": "l", "score": 0.5},
                    {"url": "https://high.example/", "title": "High", "content": "h", "score": 3},
                    {"url": "", "title": "no url"},
                    {"url": "https://none.example/", "title": "None", "content": "n"},
                    "junk",
                ]
            },
        )
    )
    scope = make(tmp_path, clock, {"web": {"searxng_url": "http://searx.local:8080/"}})
    provider = SearxngProvider()
    assert provider.missing(scope) is None
    found = asyncio.run(provider.search("q w", 2, scope=scope))
    assert [h.url for h in found] == ["https://high.example/", "https://low.example/"]
    sent = route.calls.last.request
    assert sent.url.params["q"] == "q w" and sent.url.params["format"] == "json"
    env_scope = make(tmp_path, clock, name="env", env={"SEARXNG_URL": "http://searx.local:8080"})
    assert provider.missing(env_scope) is None
    assert "searxng_url" in (provider.missing(make(tmp_path, clock, name="none")) or "")


@respx.mock
def test_searxng_adapter_failures(tmp_path: Path, clock: FakeClock) -> None:
    respx.get("http://searx.local/search").mock(
        side_effect=[
            httpx.Response(502),
            httpx.Response(200, text="<html>"),
            httpx.ConnectError("x"),
        ]
    )
    scope = make(tmp_path, clock, {"web": {"searxng_url": "http://searx.local"}})
    for expected in ["HTTP 502", "not JSON", "could not be reached"]:
        with pytest.raises(ProviderFailed) as caught:
            asyncio.run(SearxngProvider().search("q", 5, scope=scope))
        assert expected in str(caught.value)


@respx.mock
def test_brave_adapter(tmp_path: Path, clock: FakeClock) -> None:
    route = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "One", "url": "https://one.example/", "description": "first"},
                        {"title": "Two", "url": "https://two.example/", "description": "second"},
                    ]
                }
            },
        )
    )
    key = "brv" + "-fake-" + "0123456789"
    scope = make(tmp_path, clock, env={"BRAVE_SEARCH_API_KEY": key})
    provider = BraveProvider()
    found = asyncio.run(provider.search("rust", 50, scope=scope))
    assert [(h.title, h.snippet) for h in found] == [("One", "first"), ("Two", "second")]
    sent = route.calls.last.request
    assert sent.headers["x-subscription-token"] == key
    assert sent.url.params["count"] == "20"
    assert (
        provider.missing(make(tmp_path, clock, name="nokey")) == "BRAVE_SEARCH_API_KEY is not set"
    )


@respx.mock
def test_brave_adapter_without_web_section(tmp_path: Path, clock: FakeClock) -> None:
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json={"query": {}})
    )
    scope = make(tmp_path, clock, env={"BRAVE_SEARCH_API_KEY": "k" * 12})
    assert asyncio.run(BraveProvider().search("q", 5, scope=scope)) == []


def test_ddgs_provider_reports_a_missing_package(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.tools.builtin.web import providers

    monkeypatch.setattr(providers, "_ddgs_installed", lambda: False)
    provider = DuckDuckGoProvider()
    assert "ddgs" in (provider.missing(make(tmp_path, clock)) or "")
    with pytest.raises(ProviderFailed):
        asyncio.run(provider.search("q", 5, scope=None))


def test_ddgs_provider_reads_rows(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    from indus.tools.builtin.web import providers

    class FakeDDGS:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

        def __enter__(self) -> FakeDDGS:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def text(self, query: str, max_results: int) -> list[object]:
            return [
                {"title": "T1", "href": "https://1.example/", "body": "b1"},
                "junk",
                {"title": "T2", "url": "https://2.example/", "body": "b2"},
                {"title": "T3", "href": "https://3.example/", "body": "b3"},
            ]

    module = types.ModuleType("ddgs")
    module.DDGS = FakeDDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddgs", module)
    monkeypatch.setattr(providers, "_ddgs_installed", lambda: True)
    found = asyncio.run(DuckDuckGoProvider().search("q", 2, scope=None))
    assert [h.url for h in found] == ["https://1.example/", "https://2.example/"]


def test_direct_provider_fetches_through_the_safe_client(tmp_path: Path, clock: FakeClock) -> None:
    html = (
        "<html><head><title> The  Title </title><style>p{}</style></head><body>"
        "<h1>Heading</h1><p>First   paragraph\nwith a <a href='/x'>link</a>.</p>"
        "<ul><li>one</li><li>two</li></ul><script>alert(1)</script>"
        "<img src='a.png' alt='diagram'><pre>keep   spaces</pre></body></html>"
    )
    seen: list[httpx.Request] = []

    def factory(proxy: str | None) -> httpx.AsyncBaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/file.zip":
                return httpx.Response(
                    200, content=b"PK\x03\x04", headers={"content-type": "application/zip"}
                )
            if request.url.path == "/gone":
                return httpx.Response(404)
            return httpx.Response(
                200, text=html, headers={"content-type": "text/html; charset=utf-8"}
            )

        return httpx.MockTransport(handler)

    resolver = FakeResolver({"site.example": [PUBLIC]})
    provider = DirectProvider(resolver, inner_factory=factory)
    scope = make(tmp_path, clock)
    pages = asyncio.run(
        provider.extract(
            [
                "https://site.example/page",
                "https://site.example/file.zip",
                "https://site.example/gone",
            ],
            DEFAULT_CHAR_LIMIT,
            scope=scope,
        )
    )
    assert pages[0].title == "The Title"
    assert pages[0].content == (
        "# Heading\n\nFirst paragraph with a link.\n\n- one\n- two\n\n"
        "[image: diagram]\n\nkeep   spaces"
    )
    assert "application/zip" in (pages[1].error or "")
    assert "HTTP 404" in (pages[2].error or "")
    assert {r.url.host for r in seen} == {PUBLIC}
    assert {r.headers["host"] for r in seen} == {"site.example"}


def test_html_to_text_handles_broken_markup() -> None:
    title, text = html_to_text("<div><p>open <b>bold <i>both</div><table><tr><td>a<td>b</table>")
    assert title == ""
    assert "open bold both" in text and "| a | b" in text


def test_default_registry_order() -> None:
    assert default_registry().names() == ["brave", "searxng", "duckduckgo", "direct"]
    registry = default_registry()
    with pytest.raises(ValueError):
        registry.register(DirectProvider())


# -- registration --------------------------------------------------------------------------------


def test_registered_tools_dispatch_with_the_scope(tmp_path: Path, clock: FakeClock) -> None:
    provider = (
        FakeWebProvider().script(hits("https://a.example/")).serve({"https://a.example/": "A"})
    )
    tools = tools_with(provider, resolver=resolver_for(a_example=PUBLIC))
    registry = ToolRegistry(clock=clock)
    tools.register(registry)
    scope = make(tmp_path, clock)
    ctx = ToolContext(scope=scope, session_id="s1")

    async def go() -> tuple[str, str]:
        found = await dispatch(
            ToolCall("web_search", {"query": "q", "limit": "2"}, "c1"), ctx, registry=registry
        )
        page = await dispatch(
            ToolCall("web_extract", {"urls": ["https://a.example/"]}, "c2"), ctx, registry=registry
        )
        assert not found.is_error and not page.is_error
        return str(found.content), str(page.content)

    found, page = asyncio.run(go())
    assert json.loads(found)["results"][0]["url"] == "https://a.example/"
    assert json.loads(page)["results"][0]["content"] == "A"
    entry = registry.get("web_search")
    assert entry is not None and entry.traits.read_only and entry.toolset == "web"
    assert registry.available(entry, scope)


def test_schemas_are_plain_json() -> None:
    for schema in (SEARCH_SCHEMA, EXTRACT_SCHEMA):
        assert json.loads(json.dumps(schema)) == schema
        assert schema["parameters"]["required"]
    assert EXTRACT_SCHEMA["parameters"]["properties"]["urls"]["maxItems"] == 5
