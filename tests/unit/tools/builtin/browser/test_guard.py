"""The browser request guard (SSRF): every request, redirect hop and WebSocket through P05."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.safety import url_policy
from indus.tools.builtin.browser.driver import launch_proxy
from indus.tools.builtin.browser.guard import MAX_HOPS, RequestGuard
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.browser import FakeSite
from tests.unit.tools.builtin.browser._rig import make_clock, make_rig, make_scope_for

PRIVATE = {"10.0.0.5", "127.0.0.1", "169.254.169.254"}


@pytest.fixture(autouse=True)
def _fresh_blocklist() -> Any:
    url_policy.clear_blocklist_cache()
    yield
    url_policy.clear_blocklist_cache()


async def _gate(url: str) -> str | None:
    host = url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    return f"{host} is private" if host in PRIVATE else None


@dataclass
class Answer:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict[str, str])
    body: str = "ok"


def moved(to: str, status: int = 302) -> Answer:
    return Answer(status, {"Location": to}, "")


@dataclass
class FakeRequest:
    url: str
    method: str = "GET"
    navigation: bool = False

    def is_navigation_request(self) -> bool:
        return self.navigation


@dataclass
class FakeRoute:
    """Answers ``fetch`` from a table (the request's URL when none is given); records the rest."""

    request: FakeRequest
    table: dict[tuple[str, str], Answer | Exception]
    fetched: list[tuple[str, str, Any]] = field(default_factory=list[tuple[str, str, Any]])
    outcome: tuple[str, Any] | None = None

    async def fetch(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        post_data: Any = None,
        max_redirects: int | None = None,
    ) -> Any:
        assert max_redirects == 0  # redirects are never followed by the fetch itself
        target, verb = url or self.request.url, (method or self.request.method).upper()
        self.fetched.append((verb, target, post_data))
        found = self.table.get((verb, target)) or self.table.get(("*", target))
        if isinstance(found, Exception):
            raise found
        return found if found is not None else Answer()

    async def fulfill(self, *, response: Any = None) -> None:
        self.outcome = ("fulfill", response)

    async def abort(self, error_code: str | None = None) -> None:
        self.outcome = ("abort", error_code)

    async def continue_(self) -> None:
        self.outcome = ("continue", None)


def run_route(
    url: str,
    table: dict[tuple[str, str], Answer | Exception] | None = None,
    *,
    method: str = "GET",
    navigation: bool = False,
    gate: Any = _gate,
) -> tuple[FakeRoute, RequestGuard]:
    guard = RequestGuard(gate)
    route = FakeRoute(FakeRequest(url, method, navigation), dict(table or {}))
    asyncio.run(guard.route(route, route.request))
    return route, guard


def test_refused_request_is_aborted_before_anything_is_sent() -> None:
    route, guard = run_route("http://10.0.0.5/admin")
    assert route.outcome == ("abort", "blockedbyclient")
    assert route.fetched == []
    assert guard.refused[0][0] == "http://10.0.0.5/admin"


def test_local_schemes_pass_and_other_schemes_are_refused() -> None:
    assert run_route("data:text/plain,hi")[0].outcome == ("continue", None)
    assert run_route("blob:https://example.com/1")[0].outcome == ("continue", None)
    for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://10.0.0.5/"):
        route, _ = run_route(url)
        assert route.outcome == ("abort", "blockedbyclient"), url
        assert route.fetched == []


def test_resource_redirect_into_a_private_address_never_reaches_it() -> None:
    table: dict[tuple[str, str], Answer | Exception] = {
        ("GET", "https://example.com/img"): moved("https://cdn.example/next"),
        ("GET", "https://cdn.example/next"): moved("http://169.254.169.254/latest/meta-data"),
    }
    route, guard = run_route("https://example.com/img", table)
    assert route.outcome == ("abort", "blockedbyclient")
    assert [u for _, u, _ in route.fetched] == [
        "https://example.com/img",
        "https://cdn.example/next",
    ]
    assert guard.refused[-1][0] == "http://169.254.169.254/latest/meta-data"


def test_clean_resource_chain_is_followed_here_and_the_page_gets_the_last_answer() -> None:
    final = Answer(200, {"Content-Type": "image/png"}, "png")
    table: dict[tuple[str, str], Answer | Exception] = {
        ("POST", "https://example.com/api"): moved("/api/result", 303),
        ("GET", "https://example.com/api/result"): moved("https://example.com/r2", 307),
        ("GET", "https://example.com/r2"): final,
    }
    route, _ = run_route("https://example.com/api", table, method="POST")
    assert route.outcome == ("fulfill", final)  # the browser never sees a redirect
    assert route.fetched == [
        ("POST", "https://example.com/api", None),
        ("GET", "https://example.com/api/result", b""),  # 303: GET without the body
        ("GET", "https://example.com/r2", None),  # 307 keeps the method
    ]


def test_navigation_chain_is_walked_and_checked_before_the_browser_follows() -> None:
    first = moved("https://example.com/step")
    table: dict[tuple[str, str], Answer | Exception] = {
        ("GET", "https://example.com/start"): first,
        ("HEAD", "https://example.com/step"): moved("https://www.example.com/end", 301),
        ("HEAD", "https://www.example.com/end"): Answer(405),
        ("GET", "https://www.example.com/end"): Answer(200),
    }
    route, _ = run_route("https://example.com/start", table, navigation=True)
    assert route.outcome == ("fulfill", first)  # the browser follows the checked chain itself
    assert [(m, u) for m, u, _ in route.fetched] == [
        ("GET", "https://example.com/start"),
        ("HEAD", "https://example.com/step"),
        ("HEAD", "https://www.example.com/end"),
        ("GET", "https://www.example.com/end"),  # HEAD refused: asked again with GET
    ]


def test_navigation_whose_chain_ends_private_is_aborted() -> None:
    table: dict[tuple[str, str], Answer | Exception] = {
        ("GET", "https://example.com/start"): moved("https://example.com/step"),
        ("HEAD", "https://example.com/step"): moved("http://127.0.0.1:8080/admin"),
    }
    route, _ = run_route("https://example.com/start", table, navigation=True)
    assert route.outcome == ("abort", "blockedbyclient")
    assert all("127.0.0.1" not in u for _, u, _ in route.fetched)


def test_endless_redirects_stop_after_the_hop_limit() -> None:
    table: dict[tuple[str, str], Answer | Exception] = {
        ("*", f"https://example.com/{i}"): moved(f"https://example.com/{i + 1}")
        for i in range(MAX_HOPS + 5)
    }
    route, _ = run_route("https://example.com/0", table)
    assert route.outcome == ("abort", "failed")
    assert len(route.fetched) == MAX_HOPS + 1


def test_guard_fails_closed() -> None:
    async def broken(url: str) -> str | None:
        raise RuntimeError("no resolver")

    route, _ = run_route("https://example.com/", gate=broken)
    assert route.outcome == ("abort", "failed")
    assert route.fetched == []
    table: dict[tuple[str, str], Answer | Exception] = {
        ("GET", "https://example.com/"): ConnectionError("refused")
    }
    assert run_route("https://example.com/", table)[0].outcome == ("abort", "failed")


@dataclass
class FakeSocket:
    url: str
    connected: bool = False
    closed: tuple[int | None, str | None] | None = None

    async def close(self, *, code: int | None = None, reason: str | None = None) -> None:
        self.closed = (code, reason)

    def connect_to_server(self) -> Any:
        self.connected = True
        return self


@pytest.mark.parametrize(
    ("url", "connects"),
    [
        ("wss://example.com/live", True),
        ("ws://10.0.0.5:9000/", False),
        ("ws://127.0.0.1/devtools", False),
        ("http://example.com/not-a-socket", False),
    ],
)
def test_websockets_are_checked_before_they_connect(url: str, connects: bool) -> None:
    socket = FakeSocket(url)
    asyncio.run(RequestGuard(_gate).socket(socket))
    assert socket.connected is connects
    assert (socket.closed is None) is connects
    if not connects:
        assert socket.closed is not None and socket.closed[0] == 1008


# -- through the session manager and the tools (fake driver) -------------------------------------


def _navigate(rig: Any, scope: Any, url: str) -> dict[str, Any]:
    registry = ToolRegistry()
    rig.tools.register(registry)
    loop = asyncio.new_event_loop()
    try:
        ctx = ToolContext(scope=scope, task_id="t", extras={})
        result = loop.run_until_complete(
            dispatch(ToolCall("browser_navigate", {"url": url}), ctx, registry=registry)
        )
        loop.run_until_complete(rig.manager.shutdown())
    finally:
        loop.close()
    assert isinstance(result.content, str)
    return json.loads(result.content)


EMBEDS = [
    "https://example.com/logo.png",
    "http://10.0.0.5/router/reboot",
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:6379/",
]


def test_page_embeds_pass_the_scope_check(tmp_path: Path) -> None:
    rig = make_rig({"https://example.com/": FakeSite(title="Home", embeds=EMBEDS)})
    scope = make_scope_for(tmp_path, make_clock())
    assert _navigate(rig, scope, "https://example.com/")["success"] is True
    assert rig.driver.sent == ["https://example.com/logo.png"]
    assert [u for u, _ in rig.driver.blocked] == EMBEDS[1:]


def test_allow_private_urls_lets_private_embeds_through_but_never_metadata(
    tmp_path: Path,
) -> None:
    rig = make_rig({"https://example.com/": FakeSite(title="Home", embeds=EMBEDS)})
    scope = make_scope_for(tmp_path, make_clock(), {"security": {"allow_private_urls": True}})
    assert _navigate(rig, scope, "https://example.com/")["success"] is True
    assert [u for u, _ in rig.driver.blocked] == ["http://169.254.169.254/latest/meta-data/"]
    assert "http://10.0.0.5/router/reboot" in rig.driver.sent


def test_attached_browser_is_guarded_too(tmp_path: Path) -> None:
    rig = make_rig({"https://example.com/": FakeSite(title="Home", embeds=EMBEDS)})
    scope = make_scope_for(
        tmp_path, make_clock(), {"browser": {"cdp_url": "http://127.0.0.1:9222"}}
    )
    assert _navigate(rig, scope, "https://example.com/")["success"] is True
    assert rig.driver.attaches == ["http://127.0.0.1:9222"]
    assert rig.driver.sent == ["https://example.com/logo.png"]


def test_a_browser_without_its_guard_is_closed_and_never_used(tmp_path: Path) -> None:
    rig = make_rig({"https://example.com/": FakeSite(title="Home", embeds=EMBEDS)})
    rig.driver.guard_failures = 1
    scope = make_scope_for(tmp_path, make_clock())
    body = _navigate(rig, scope, "https://example.com/")
    assert body["success"] is False
    assert rig.driver.closed == rig.driver.handles  # the unguarded browser was closed
    assert rig.driver.sent == [] and rig.driver.blocked == []
    profiles = scope.home / "cache" / "browser" / "profiles"
    assert not profiles.exists() or list(profiles.iterdir()) == []


def test_launch_proxy_from_a_passthrough_environment() -> None:
    user = "u" + "ser"
    word = "p" + "w"
    env = {"HTTPS_PROXY": f"http://{user}:{word}@proxy.example:3128", "NO_PROXY": "localhost"}
    assert launch_proxy(env) == {
        "server": "http://proxy.example:3128",
        "username": user,
        "password": word,
        "bypass": "localhost",
    }
    assert launch_proxy({"http_proxy": "proxy.example:8080"}) == {
        "server": "http://proxy.example:8080"
    }
    assert launch_proxy({"PATH": "/usr/bin"}) is None
