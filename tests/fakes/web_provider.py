"""A scripted web provider for the web tool tests (no network).

``FakeWebProvider`` answers ``search`` from a queue of scripted answers (a list of hits or an
exception; the last one repeats) and ``extract`` per URL from a table (a ``Page``, a text body or
an exception for the whole call). It counts calls, records the arguments, and can hold every call
at a gate (an ``asyncio.Event``) so tests can line up concurrent searches.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from indus.foundation.scope import Scope
from indus.tools.builtin.web.providers import Page, ProviderFailed, SearchHit, WebProvider


def hits(*urls: str) -> list[SearchHit]:
    return [SearchHit(f"Title {n}", url, f"About {url}") for n, url in enumerate(urls, start=1)]


def many_hits(count: int, prefix: str = "https://r.example/") -> list[SearchHit]:
    return hits(*(f"{prefix}{n}" for n in range(count)))


class FakeWebProvider(WebProvider):
    def __init__(
        self,
        name: str = "fake",
        *,
        keyless: bool = False,
        search: bool = True,
        extract: bool = True,
        missing: str | None = None,
    ) -> None:
        self.name = name
        self.title = name
        self.keyless = keyless
        self.can_search = search
        self.can_extract = extract
        self.why_missing = missing
        self.answers: list[list[SearchHit] | BaseException] = [[]]
        self.pages: dict[str, Page | str] = {}
        self.extract_error: BaseException | None = None
        self.extract_reply: list[Page] | None = None
        self.search_calls: list[tuple[str, int]] = []
        self.extract_calls: list[tuple[list[str], int]] = []
        self.gate: asyncio.Event | None = None
        self.entered = 0

    def script(self, *answers: list[SearchHit] | BaseException) -> FakeWebProvider:
        self.answers = list(answers)
        return self

    def serve(self, pages: Mapping[str, Page | str]) -> FakeWebProvider:
        self.pages.update(pages)
        return self

    def missing(self, scope: Scope | None) -> str | None:
        return self.why_missing

    async def _wait(self) -> None:
        self.entered += 1
        if self.gate is not None:
            await self.gate.wait()

    async def search(self, query: str, limit: int, *, scope: Scope | None) -> list[SearchHit]:
        self.search_calls.append((query, limit))
        await self._wait()
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)

    async def extract(
        self, urls: Sequence[str], char_limit: int, *, scope: Scope | None
    ) -> list[Page]:
        self.extract_calls.append((list(urls), char_limit))
        await self._wait()
        if self.extract_error is not None:
            raise self.extract_error
        if self.extract_reply is not None:
            return list(self.extract_reply)
        out: list[Page] = []
        for url in urls:
            page = self.pages.get(url)
            if page is None:
                out.append(Page(url, error="not found"))
            elif isinstance(page, str):
                out.append(Page(url, title=f"Page {url}", content=page))
            else:
                out.append(page)
        return out


def failing(message: str = "vendor down") -> ProviderFailed:
    return ProviderFailed(message)
