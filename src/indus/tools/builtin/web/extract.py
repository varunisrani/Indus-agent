# pyright: strict
"""``web_extract``: URL gates, the provider call, order, and the page budget.

Steps for one call:

1. take at most ``MAX_URLS`` items; an item may be a URL or a search hit (``{"url": …}`` /
   ``{"href": …}``); anything else gets its own error entry;
2. normalise each URL (``url_policy.normalize_url``) and check it (``url_policy.check_url``): a
   refused URL gets an error entry saying why and is never sent to a provider;
3. send the rest to the chosen provider in one call, bounded by ``PROVIDER_TIMEOUT_S`` on the
   profile's clock; a timeout or a provider failure becomes an error entry per URL;
4. match the provider's pages back to the URLs asked (by URL, then by position when the counts
   agree) and restore the order of the request;
5. per page: refuse raw binary payloads, replace inline base64 images with ``[image: alt]``, and cut
   a page over ``char_limit`` to a head and a tail with a footer that names the stored full text
   (``<home>/cache/web/``, at most ``MAX_STORED_CHARS``) and the ``read_file`` call for the middle.

When no entry has any content the call fails as "inaccessible" (the per-URL reasons are kept).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.fsio import atomic_write_text
from indus.foundation.scope import Scope
from indus.safety.url_policy import Blocked, Resolver, check_url_async, normalize_url
from indus.tools.builtin.web.providers import NoProvider, Page, ProviderRegistry, choose

__all__ = [
    "DEFAULT_CHAR_LIMIT",
    "MAX_STORED_CHARS",
    "MAX_URLS",
    "PROVIDER_TIMEOUT_S",
    "binary_kind",
    "clamp_char_limit",
    "cut_page",
    "replace_inline_images",
    "run_extract",
    "store_full_text",
]

log = logging.getLogger(__name__)

MAX_URLS = 5
DEFAULT_CHAR_LIMIT = 15_000
MIN_CHAR_LIMIT = 2_000
MAX_CHAR_LIMIT = 500_000
MAX_STORED_CHARS = 2_000_000
PROVIDER_TIMEOUT_S = 120.0
HEAD_SHARE = 0.75
WEB_CACHE = Path("cache") / "web"
READ_WINDOW_LINES = 200


def clamp_char_limit(value: object) -> int:
    if value is None or isinstance(value, bool):
        return DEFAULT_CHAR_LIMIT
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_CHAR_LIMIT
    return max(MIN_CHAR_LIMIT, min(number, MAX_CHAR_LIMIT))


# -- page text ----------------------------------------------------------------------------------

_MD_DATA_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]*\)"
)
_WRAPPED_DATA_IMAGE = re.compile(r"\(\s*data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]*\)")
_BARE_DATA_IMAGE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")


def replace_inline_images(text: str) -> str:
    """Inline base64 images (a context flood) become ``[image: alt]`` / ``[image]``; linked
    images (``http(s)``) stay as they are."""

    def markdown(match: re.Match[str]) -> str:
        alt = match.group("alt").strip()
        return f"[image: {alt}]" if alt else "[image]"

    text = _MD_DATA_IMAGE.sub(markdown, text)
    text = _WRAPPED_DATA_IMAGE.sub("[image]", text)
    return _BARE_DATA_IMAGE.sub("[image]", text)


# File signatures that never start a readable page (compared with NUL bytes dropped, since
# providers hand binary bodies back as text without them).
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"SQLite format 3", "SQLite database"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"BZh", "bzip2 archive"),
    (b"\xfd7zXZ", "xz archive"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    (b"\x7fELF", "ELF executable"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"%PDF-", "PDF document"),
    (b"OggS", "Ogg media"),
    (b"fLaC", "FLAC audio"),
)


def binary_kind(text: str) -> str | None:
    """The file type when ``text`` is a raw binary body rather than a page, else ``None``."""
    head = text[:32].encode("latin-1", "ignore").replace(b"\x00", b"")
    for signature, kind in _SIGNATURES:
        if head.startswith(signature.replace(b"\x00", b"")):
            return kind
    return None


def _slug(url: str) -> str:
    try:
        host = urlsplit(url).hostname or "page"
    except ValueError:
        host = "page"
    return re.sub(r"[^A-Za-z0-9._-]", "-", host)[:60].strip("-.") or "page"


def store_full_text(home: Path, url: str, text: str) -> Path | None:
    """Save ``text`` (bounded) under ``<home>/cache/web/``; ``None`` when that fails."""
    folder = home / WEB_CACHE
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    path = folder / f"{_slug(url)}-{digest}.md"
    body = text
    if len(body) > MAX_STORED_CHARS:
        body = body[:MAX_STORED_CHARS] + (
            f"\n\n[stored copy stops at {MAX_STORED_CHARS:,} of {len(text):,} characters; "
            "extract a more specific URL for the rest]\n"
        )
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_text(path, body, mode=0o600)
    except OSError as exc:
        log.debug("could not store the full text of %s: %s", url, exc)
        return None
    return path


def cut_page(text: str, char_limit: int, stored: Path | None) -> str:
    """``text`` cut to a head and a tail (on line ends) with a footer; ``text`` when it fits."""
    if len(text) <= char_limit:
        return text
    head_budget = int(char_limit * HEAD_SHARE)
    tail_budget = char_limit - head_budget
    head, tail = text[:head_budget], text[-tail_budget:]
    newline = head.rfind("\n")
    if newline > head_budget // 2:
        head = head[:newline]
    newline = tail.find("\n")
    if 0 <= newline < tail_budget // 2:
        tail = tail[newline + 1 :]
    lines = [
        "",
        "==== page cut ====",
        f"Shown: the first {len(head):,} and the last {len(tail):,} of {len(text):,} characters.",
    ]
    if stored is not None:
        first_hidden = head.count("\n") + 2  # read_file lines start at 1
        lines += [
            f"Full text saved at: {stored}",
            f'Read the part left out with read_file path="{stored}" offset={first_hidden} '
            f"limit={READ_WINDOW_LINES} (move offset to page through the whole file).",
        ]
    else:
        lines.append("The full text could not be saved; extract a more specific URL instead.")
    lines.append("==================")
    return (
        f"{head}\n\n[... the middle of the page is left out; see below ...]\n\n{tail}\n"
        + "\n".join(lines)
    )


# -- the call -----------------------------------------------------------------------------------


def _url_of(item: object) -> str | None:
    if isinstance(item, dict):
        mapping = cast(dict[str, Any], item)
        item = mapping.get("url") or mapping.get("href")
    if isinstance(item, str) and item.strip():
        return item.strip()
    return None


def _items(raw: object) -> list[object] | None:
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed: object = json.loads(text)
            except ValueError:
                return [raw]
            return cast(list[object], parsed) if isinstance(parsed, list) else None
        return [raw]
    if isinstance(raw, list):
        return cast(list[object], raw)
    return None


async def _bounded(awaitable: Awaitable[list[Page]], clock: Clock, timeout_s: float) -> list[Page]:
    work = asyncio.ensure_future(awaitable)
    timer = asyncio.ensure_future(clock.sleep(timeout_s))
    try:
        done, _ = await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
        if not work.done():
            work.cancel()
    if work in done:
        return work.result()
    raise TimeoutError


def _match(asked: Sequence[str], pages: Sequence[Page]) -> list[Page]:
    by_url: dict[str, Page] = {}
    for page in pages:
        by_url.setdefault(page.url, page)
    same_count = len(pages) == len(asked)
    matched: list[Page] = []
    for position, url in enumerate(asked):
        page = by_url.get(url)
        if page is None and same_count and pages[position].url not in asked:
            page = replace(pages[position], url=url)  # the provider reported a redirect target
        matched.append(page or Page(url, error="the provider returned nothing for this URL"))
    return matched


def _finish(page: Page, char_limit: int, home: Path | None) -> Page:
    if page.error is not None or not page.content:
        return page
    kind = binary_kind(page.content)
    if kind is not None:
        return Page(
            page.url,
            page.title,
            "",
            f"the URL returned a {kind}, not a page; download it with the terminal and open it "
            "with read_file",
        )
    text = replace_inline_images(page.content)
    if len(text) <= char_limit:
        return replace(page, content=text)
    stored = store_full_text(home, page.url, text) if home is not None else None
    return replace(page, content=cut_page(text, char_limit, stored))


async def run_extract(
    registry: ProviderRegistry,
    args: dict[str, Any],
    scope: Scope | None,
    *,
    resolver: Resolver | None = None,
    clock: Clock | None = None,
    timeout_s: float = PROVIDER_TIMEOUT_S,
) -> dict[str, Any]:
    """The ``web_extract`` result as a dict (``{"error": …}`` on failure)."""
    items = _items(args.get("urls"))
    if not items:
        return {"error": "web_extract needs urls: a list of 1 to 5 URLs"}
    note = None
    if len(items) > MAX_URLS:
        note = f"only the first {MAX_URLS} URLs were extracted; ask again for the rest"
        items = items[:MAX_URLS]
    char_limit = clamp_char_limit(args.get("char_limit"))

    entries: list[Page | None] = [None] * len(items)
    wanted: list[tuple[int, str]] = []
    for index, item in enumerate(items):
        text = _url_of(item)
        if text is None:
            entries[index] = Page("", error=f"item {index} is not a URL (give a URL string)")
            continue
        url = normalize_url(text)
        verdict = await check_url_async(url, scope, resolver=resolver)
        if isinstance(verdict, Blocked):
            entries[index] = Page(url, error=f"Blocked: {verdict.reason}")
        else:
            wanted.append((index, url))

    if wanted:
        try:
            provider = choose(registry, scope, "extract")
        except NoProvider as exc:
            return {"error": str(exc)}
        urls = [url for _, url in wanted]
        use_clock = clock or (scope.clock if scope is not None else SystemClock())
        try:
            pages = await _bounded(
                provider.extract(urls, char_limit, scope=scope), use_clock, timeout_s
            )
        except TimeoutError:
            log.warning("web_extract: %s gave no answer in %.0f s", provider.name, timeout_s)
            pages = [
                Page(u, error=f"{provider.name} gave no answer within {timeout_s:.0f} s")
                for u in urls
            ]
        except Exception as exc:
            pages = [Page(u, error=f"{provider.name} failed: {exc}") for u in urls]
        for (index, _), page in zip(wanted, _match(urls, pages), strict=True):
            entries[index] = page

    home = scope.home if scope is not None else None
    finished = [_finish(page, char_limit, home) for page in entries if page is not None]
    results = [
        {"url": p.url, "title": p.title, "content": p.content, "error": p.error} for p in finished
    ]
    body: dict[str, Any] = {"results": results}
    if note:
        body["note"] = note
    if not any(p.content for p in finished):
        reasons = "; ".join(f"{p.url or '?'}: {p.error or 'empty page'}" for p in finished)
        return {"error": f"the content was inaccessible ({reasons})", **body}
    return body
