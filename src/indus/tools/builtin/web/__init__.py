# pyright: strict
"""The ``web_search`` and ``web_extract`` tools (toolset ``web``).

``WebTools`` holds what the two handlers share in a process: the provider registry, the search
memo and the resolver the URL policy uses. ``register_tools`` registers both tools on the module's
default instance; tests build their own instance with fake providers and register that.

Results are JSON text. The tool round fences both tools' results as untrusted data (P04-13); this
package does not wrap them itself.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.safety.url_policy import Resolver
from indus.tools.builtin.web.extract import (
    DEFAULT_CHAR_LIMIT,
    MAX_URLS,
    MIN_CHAR_LIMIT,
    run_extract,
)
from indus.tools.builtin.web.providers import NoProvider, ProviderRegistry, choose, default_registry
from indus.tools.builtin.web.search import DEFAULT_LIMIT, MAX_LIMIT, SearchMemo, run_search
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = ["EXTRACT_SCHEMA", "SEARCH_SCHEMA", "TOOLSET", "WebTools", "register_tools"]

TOOLSET = "web"
MAX_RESULT_CHARS = 100_000


def _object(required: list[str], **properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": properties}


SEARCH_SCHEMA: dict[str, Any] = {
    "name": "web_search",
    "description": (
        "Look something up on the web. Gives back a ranked list of pages: title, address and a "
        f"short snippet for each ({DEFAULT_LIMIT} by default). It does not open the pages; pass "
        "the addresses you want to read to web_extract. The query goes to the search provider "
        "as written, so operators it understands (site:, quotes, a leading minus) can be used."
    ),
    "parameters": _object(
        ["query"],
        query={"description": "What to search for.", "type": "string"},
        limit={
            "description": f"How many results to return, 1 to {MAX_LIMIT}.",
            "type": "integer",
            "default": DEFAULT_LIMIT,
            "minimum": 1,
            "maximum": MAX_LIMIT,
        },
    ),
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "name": "web_extract",
    "description": (
        f"Read the text of up to {MAX_URLS} web pages (or documents such as PDFs, when the "
        "provider supports them). Each page comes back whole when it fits the character budget "
        f"(default {DEFAULT_CHAR_LIMIT:,}); a longer page comes back as its beginning and end, "
        "with a note giving the saved file that holds all of it and the read_file call that "
        "shows the part left out. Embedded images show as [image: description]. Internal "
        "network addresses and links that carry credentials are refused per address."
    ),
    "parameters": _object(
        ["urls"],
        urls={
            "description": f"The page addresses to read, at most {MAX_URLS}.",
            "maxItems": MAX_URLS,
            "type": "array",
            "items": {"type": "string"},
        },
        char_limit={
            "description": (
                "Characters of each page to send back before it is cut "
                f"(default {DEFAULT_CHAR_LIMIT:,}). Raise it to see more of a long page."
            ),
            "type": "integer",
            "minimum": MIN_CHAR_LIMIT,
        },
    ),
}


def _json(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


class WebTools:
    """The shared state of the web tools in one process (see the module notes)."""

    def __init__(
        self,
        providers: ProviderRegistry | None = None,
        *,
        memo: SearchMemo | None = None,
        resolver: Resolver | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.resolver = resolver
        self.providers = providers if providers is not None else default_registry(resolver)
        self.memo = memo if memo is not None else SearchMemo()
        self.clock = clock

    def search_ready(self, scope: Scope) -> bool:
        """Tool probe: a provider can search here, or the user named one (its error then shows)."""
        try:
            choose(self.providers, scope, "search")
        except NoProvider as exc:
            return exc.explicit
        return True

    async def search(self, args: Mapping[str, Any], *, scope: Scope | None = None) -> str:
        body = await run_search(self.providers, self.memo, dict(args), scope, clock=self.clock)
        return _json(body)

    async def extract(self, args: Mapping[str, Any], *, scope: Scope | None = None) -> str:
        body = await run_extract(
            self.providers, dict(args), scope, resolver=self.resolver, clock=self.clock
        )
        return _json(body)

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "web_search",
            TOOLSET,
            SEARCH_SCHEMA,
            self.search,
            probe=self.search_ready,
            max_result_chars=MAX_RESULT_CHARS,
            traits=ToolTraits(read_only=True),
        )
        registry.register(
            "web_extract",
            TOOLSET,
            EXTRACT_SCHEMA,
            self.extract,
            max_result_chars=MAX_RESULT_CHARS,
            traits=ToolTraits(read_only=True),
        )


DEFAULT_TOOLS = WebTools()


def register_tools(registry: ToolRegistry) -> None:
    DEFAULT_TOOLS.register(registry)
