"""W60-L2-1: the key and address variables our tools declare, as one list per backend."""

from __future__ import annotations

from indus.tools.builtin.browser.cloud import PROVIDERS
from indus.tools.builtin.web.providers import BRAVE_KEY, SEARXNG_ENV
from indus.tools.key_needs import KeyNeed, tool_key_needs
from indus.tools.registry import ToolRegistry
from tests.fakes.clock import FakeClock
from tests.unit.tools._helpers import echo, schema


def test_built_in_backends_list_the_names_their_code_reads() -> None:
    rows = {(row.toolset, row.backend): row for row in tool_key_needs()}
    assert rows[("web", "brave")].needs == (KeyNeed(BRAVE_KEY, "key"),)
    assert rows[("web", "searxng")].needs == (KeyNeed(SEARXNG_ENV, "url"),)
    for provider in PROVIDERS.values():
        names = [need.name for need in rows[("browser", provider.name)].needs]
        assert names == [*provider.secret_names, *provider.url_names]
    assert rows[("browser", "camofox")].needs == (KeyNeed("CAMOFOX_API_KEY", "key"),)
    assert rows[("browser", "cdp")].needs == (KeyNeed("INDUS_BROWSER_CDP_URL", "url"),)
    # keyless providers (DuckDuckGo, direct fetch) have nothing to list
    assert ("web", "duckduckgo") not in rows and ("web", "direct") not in rows


def test_registered_tools_add_their_required_secrets(fake_clock: FakeClock) -> None:
    registry = ToolRegistry(clock=fake_clock)
    registry.register("weather_now", "weather", schema("weather_now"), echo)
    registry.register(
        "stock_quote", "markets", schema("stock_quote"), echo, requires_secrets=("QUOTES_KEY",)
    )
    extra = [row for row in tool_key_needs(registry) if row.toolset == "markets"]
    assert [(r.backend, r.needs) for r in extra] == [
        ("stock_quote", (KeyNeed("QUOTES_KEY", "key"),))
    ]
    assert not any(row.backend == "weather_now" for row in tool_key_needs(registry))
