# pyright: strict
"""Web providers: one interface for search and page extraction, a registry, and the built-ins.

A provider says what it can do (``can_search`` / ``can_extract``), whether it needs no key
(``keyless``: it may serve a one-shot rescue when a keyed provider fails), and why it is not usable
for a profile right now (``missing(scope)``, ``None`` when it is). ``search`` returns hits;
``extract`` returns one ``Page`` per URL it was given. Either raises ``ProviderFailed`` when the
provider as a whole could not answer; a single page that failed is a ``Page`` with ``error``.

Built-ins in P05: ``brave`` (Brave Search API, key ``BRAVE_SEARCH_API_KEY``), ``searxng`` (a
user-hosted SearXNG instance, ``web.searxng_url`` or ``SEARXNG_URL``), ``duckduckgo`` (keyless,
through the optional ``ddgs`` package, extra ``web``) and ``direct`` (keyless extraction: fetch
the page through the SSRF-safe client and turn its HTML into text). Further vendors (Exa, Tavily,
Firecrawl, …) arrive as plugins (P13) behind the same interface.

Choosing (``choose``): ``web.search_backend`` / ``web.extract_backend`` name a provider strictly;
``web.backend`` names one for every capability it has; with nothing named, the first usable
provider in registry order serves. A provider the user named that is unknown, cannot do the job or
is not usable is an error naming it — never a quiet switch to another one.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal, cast

import httpx

from indus.foundation.blocking import DeadlineMissed, run_blocking
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.safety.safe_transport import TransportFactory, safe_client
from indus.safety.url_policy import Resolver

__all__ = [
    "BraveProvider",
    "DirectProvider",
    "DuckDuckGoProvider",
    "NoProvider",
    "Page",
    "ProviderFailed",
    "ProviderRegistry",
    "SearchHit",
    "SearxngProvider",
    "WebProvider",
    "choose",
    "default_registry",
    "html_to_text",
]

log = logging.getLogger(__name__)

type Capability = Literal["search", "extract"]

HTTP_TIMEOUT_S = 15.0
DDG_DEADLINE_S = 30.0
FETCH_TIMEOUT_S = 30.0
MAX_FETCH_BYTES = 5_000_000
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_MAX_COUNT = 20
BRAVE_KEY = "BRAVE_SEARCH_API_KEY"
SEARXNG_ENV = "SEARXNG_URL"


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class Page:
    url: str
    title: str = ""
    content: str = ""
    error: str | None = None


class ProviderFailed(Exception):
    """The provider could not answer at all (network, HTTP status, unreadable reply)."""


class NoProvider(Exception):
    """No provider can serve the capability; the message says what to change. ``explicit`` is
    true when the user named a provider that cannot serve (an error to show, not a missing tool)."""

    def __init__(self, message: str, *, explicit: bool = False) -> None:
        super().__init__(message)
        self.explicit = explicit


class WebProvider:
    """Base for providers; subclasses override what they support."""

    name: str = ""
    title: str = ""
    keyless: bool = False
    can_search: bool = False
    can_extract: bool = False

    def can(self, capability: Capability) -> bool:
        return self.can_search if capability == "search" else self.can_extract

    def missing(self, scope: Scope | None) -> str | None:
        """Why this provider is not usable for ``scope``; ``None`` when it is."""
        return None

    def available(self, scope: Scope | None) -> bool:
        return self.missing(scope) is None

    async def search(self, query: str, limit: int, *, scope: Scope | None) -> list[SearchHit]:
        raise ProviderFailed(f"{self.name} cannot search")

    async def extract(
        self, urls: Sequence[str], char_limit: int, *, scope: Scope | None
    ) -> list[Page]:
        raise ProviderFailed(f"{self.name} cannot extract pages")


class ProviderRegistry:
    """Providers by name, in registration order (the order ``choose`` tries them in)."""

    def __init__(self, providers: Iterable[WebProvider] = ()) -> None:
        self._providers: dict[str, WebProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: WebProvider, *, replace: bool = False) -> None:
        if not provider.name:
            raise ValueError("a web provider needs a name")
        if provider.name in self._providers and not replace:
            raise ValueError(f"web provider {provider.name!r} is already registered")
        self._providers[provider.name] = provider

    def get(self, name: str) -> WebProvider | None:
        return self._providers.get(name.strip().lower())

    def names(self) -> list[str]:
        return list(self._providers)

    def all(self) -> list[WebProvider]:
        return list(self._providers.values())


# -- choosing -----------------------------------------------------------------------------------


def config_text(scope: Scope | None, key: str) -> str:
    value: object = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            log.debug("config %s unreadable", key, exc_info=True)
    if value is None:
        value = default_for(key, "")
    return str(value).strip() if value is not None else ""


def config_flag(scope: Scope | None, key: str) -> bool:
    value: object = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            log.debug("config %s unreadable", key, exc_info=True)
    if value is None:
        value = default_for(key, False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _verb(capability: Capability) -> str:
    return "search the web" if capability == "search" else "extract pages"


def choose(registry: ProviderRegistry, scope: Scope | None, capability: Capability) -> WebProvider:
    """The provider that serves ``capability`` for ``scope`` (see the module notes)."""
    own_key = f"web.{capability}_backend"
    own = config_text(scope, own_key).lower()
    shared = config_text(scope, "web.backend").lower()
    known = ", ".join(registry.names()) or "none"
    for key, name, strict in ((own_key, own, True), ("web.backend", shared, False)):
        if not name:
            continue
        provider = registry.get(name)
        if provider is None:
            raise NoProvider(
                f"{key} is {name!r}, which is not a known web provider (known: {known})",
                explicit=True,
            )
        if not provider.can(capability):
            if not strict:
                continue  # the shared choice covers only what the provider can do
            able = ", ".join(p.name for p in registry.all() if p.can(capability)) or "none"
            raise NoProvider(
                f"{key} is {name!r}, which cannot {_verb(capability)}; providers that can: {able}",
                explicit=True,
            )
        reason = provider.missing(scope)
        if reason is not None:
            raise NoProvider(
                f"{key} selects {name!r}, but it is not usable: {reason}", explicit=True
            )
        return provider
    for provider in registry.all():
        if provider.can(capability) and provider.available(scope):
            return provider
    hints = [f"{p.name}: {p.missing(scope)}" for p in registry.all() if p.can(capability)] or [
        "none is registered"
    ]
    raise NoProvider(f"no web provider can {_verb(capability)} here ({'; '.join(hints)})")


def keyless_for(
    registry: ProviderRegistry, scope: Scope | None, capability: Capability, *, besides: str
) -> WebProvider | None:
    """The first usable keyless provider other than ``besides`` (the rescue candidate)."""
    for provider in registry.all():
        if provider.name == besides or not provider.keyless or not provider.can(capability):
            continue
        if provider.available(scope):
            return provider
    return None


# -- shared HTTP helpers ------------------------------------------------------------------------


async def _get_json(
    label: str, url: str, *, params: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, trust_env=True) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderFailed(f"{label} could not be reached: {type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400:
        raise ProviderFailed(f"{label} answered HTTP {response.status_code}")
    try:
        body: object = response.json()
    except ValueError as exc:
        raise ProviderFailed(f"{label} sent a reply that is not JSON") from exc
    if not isinstance(body, dict):
        raise ProviderFailed(f"{label} sent an unexpected reply")
    return cast(dict[str, Any], body)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _secret(scope: Scope | None, name: str) -> str:
    if scope is None:
        return ""
    value: str | None = None
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception:
            log.debug("secret %s unreadable", name, exc_info=True)
    if not value:
        value = scope.env.get(name)
    return (value or "").strip()


# -- built-in providers -------------------------------------------------------------------------


class BraveProvider(WebProvider):
    name = "brave"
    title = "Brave Search"
    can_search = True

    def missing(self, scope: Scope | None) -> str | None:
        return None if _secret(scope, BRAVE_KEY) else f"{BRAVE_KEY} is not set"

    async def search(self, query: str, limit: int, *, scope: Scope | None) -> list[SearchHit]:
        key = _secret(scope, BRAVE_KEY)
        if not key:
            raise ProviderFailed(f"{BRAVE_KEY} is not set")
        body = await _get_json(
            self.title,
            BRAVE_ENDPOINT,
            params={"q": query, "count": max(1, min(limit, BRAVE_MAX_COUNT))},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )
        web: object = body.get("web")
        rows: object = cast(dict[str, Any], web).get("results") if isinstance(web, dict) else None
        hits: list[SearchHit] = []
        for row in cast(list[object], rows) if isinstance(rows, list) else []:
            if isinstance(row, dict):
                item = cast(dict[str, Any], row)
                url = _text(item.get("url"))
                if url:
                    hits.append(
                        SearchHit(_text(item.get("title")), url, _text(item.get("description")))
                    )
        return hits[:limit]


class SearxngProvider(WebProvider):
    name = "searxng"
    title = "SearXNG"
    can_search = True

    @staticmethod
    def base_url(scope: Scope | None) -> str:
        url = config_text(scope, "web.searxng_url")
        if not url and scope is not None:
            url = (scope.env.get(SEARXNG_ENV) or "").strip()
        return url.rstrip("/")

    def missing(self, scope: Scope | None) -> str | None:
        return None if self.base_url(scope) else f"web.searxng_url (or {SEARXNG_ENV}) is not set"

    async def search(self, query: str, limit: int, *, scope: Scope | None) -> list[SearchHit]:
        base = self.base_url(scope)
        if not base:
            raise ProviderFailed("web.searxng_url is not set")
        body = await _get_json(
            f"SearXNG at {base}",
            f"{base}/search",
            params={"q": query, "format": "json", "pageno": 1},
            headers={"Accept": "application/json"},
        )
        rows: object = body.get("results")
        items = [
            cast(dict[str, Any], r)
            for r in (cast(list[object], rows) if isinstance(rows, list) else [])
            if isinstance(r, dict)
        ]

        def score(item: dict[str, Any]) -> float:
            try:
                return float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        items.sort(key=score, reverse=True)  # stable: equal scores keep the instance's order
        hits = [
            SearchHit(_text(i.get("title")), _text(i.get("url")), _text(i.get("content")))
            for i in items
            if _text(i.get("url"))
        ]
        return hits[:limit]


def _ddgs_installed() -> bool:
    return importlib.util.find_spec("ddgs") is not None


class DuckDuckGoProvider(WebProvider):
    name = "duckduckgo"
    title = "DuckDuckGo"
    keyless = True
    can_search = True

    def missing(self, scope: Scope | None) -> str | None:
        return None if _ddgs_installed() else "the ddgs package is not installed (extra 'web')"

    async def search(self, query: str, limit: int, *, scope: Scope | None) -> list[SearchHit]:
        if not _ddgs_installed():
            raise ProviderFailed("the ddgs package is not installed (extra 'web')")

        def run() -> list[SearchHit]:
            module: Any = importlib.import_module("ddgs")
            found: list[SearchHit] = []
            with module.DDGS(timeout=10) as client:
                rows: list[object] = list(client.text(query, max_results=limit) or [])
                for row in rows:
                    if len(found) >= limit:
                        break
                    if isinstance(row, dict):
                        item = cast(dict[str, Any], row)
                        url = _text(item.get("href")) or _text(item.get("url"))
                        if url:
                            found.append(
                                SearchHit(_text(item.get("title")), url, _text(item.get("body")))
                            )
            return found

        try:
            return await run_blocking(run, pool="io", deadline=DDG_DEADLINE_S, label="ddgs search")
        except DeadlineMissed as exc:
            raise ProviderFailed(
                f"DuckDuckGo did not answer within {DDG_DEADLINE_S:.0f} s (it may be rate limiting)"
            ) from exc
        except ProviderFailed:
            raise
        except Exception as exc:
            raise ProviderFailed(f"DuckDuckGo search failed: {type(exc).__name__}: {exc}") from exc


# -- direct extraction --------------------------------------------------------------------------

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe", "form"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "ul", "ol",
        "table", "tr", "blockquote", "pre", "figure", "figcaption", "dl", "dt", "dd", "hr",
    }
)  # fmt: skip
_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_SPACES = re.compile(r"[ \t\f\v\r]+")


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._in_title = False
        self._in_pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _HEADINGS:
            self.parts.append("\n\n" + "#" * _HEADINGS[tag] + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self.parts.append(f"[image: {alt}]")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")
        if tag == "pre":
            self._in_pre += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "pre":
            self._in_pre = max(0, self._in_pre - 1)
        if tag in _HEADINGS or tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip:
            return
        self.parts.append(data if self._in_pre else _SPACES.sub(" ", data.replace("\n", " ")))


def html_to_text(html: str) -> tuple[str, str]:
    """``(title, text)`` of an HTML document: readable blocks, headings as ``#`` lines, list items
    as ``-`` lines; scripts, styles and forms dropped."""
    collector = _TextCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # HTMLParser is lenient; anything left is best effort
        log.debug("HTML parse stopped early", exc_info=True)
    lines = [line.strip() for line in "".join(collector.parts).split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    title = _SPACES.sub(" ", " ".join(collector.title_parts)).strip()
    return title, "\n".join(out).strip()


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    return match.group(1) if match else "utf-8"


class DirectProvider(WebProvider):
    """Keyless extraction: fetch each page through the SSRF-safe client and read its text."""

    name = "direct"
    title = "Direct fetch"
    keyless = True
    can_extract = True

    def __init__(
        self, resolver: Resolver | None = None, *, inner_factory: TransportFactory | None = None
    ) -> None:
        self.resolver = resolver
        self.inner_factory = inner_factory

    async def extract(
        self, urls: Sequence[str], char_limit: int, *, scope: Scope | None
    ) -> list[Page]:
        client = safe_client(
            scope,
            resolver=self.resolver,
            inner_factory=self.inner_factory,
            timeout=FETCH_TIMEOUT_S,
            headers={"User-Agent": "IndusAgent/0 (+web_extract)", "Accept": "text/html,*/*;q=0.5"},
        )
        async with client:
            return list(await asyncio.gather(*(self._one(client, url) for url in urls)))

    async def _one(self, client: httpx.AsyncClient, url: str) -> Page:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return Page(url, error=f"the site answered HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_FETCH_BYTES:
                        break
                content_type = response.headers.get("content-type", "").lower()
        except httpx.HTTPError as exc:
            return Page(url, error=f"could not fetch the page: {exc}")
        raw = bytes(body[:MAX_FETCH_BYTES])
        if "html" in content_type or (not content_type and raw.lstrip()[:1] == b"<"):
            title, text = html_to_text(raw.decode(_charset(content_type), errors="replace"))
            return Page(url, title=title, content=text)
        if content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
            return Page(url, content=raw.decode(_charset(content_type), errors="replace"))
        kind = content_type.split(";", 1)[0] or "unknown"
        return Page(
            url,
            error=f"the URL returned {kind} content, not a page; download it with the terminal "
            "and open it with read_file",
        )


def default_registry(resolver: Resolver | None = None) -> ProviderRegistry:
    """The built-in providers in auto-selection order (keyed first, keyless last)."""
    return ProviderRegistry(
        [BraveProvider(), SearxngProvider(), DuckDuckGoProvider(), DirectProvider(resolver)]
    )
