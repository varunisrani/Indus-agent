"""``llms.txt`` and ``llms-full.txt`` for the docs site, written at the end of every build (P25-14).

``mkdocs.yml`` loads this module as a hook; ``on_post_build`` reads the navigation and the page
sources and writes both files into the built site:

- ``llms.txt``: the site name, its one-line description, then one ``## <section>`` per top-level
  navigation entry with a ``- [title](url): summary`` line per page (the summary is the first
  sentence of the page's first paragraph);
- ``llms-full.txt``: every page's Markdown in navigation order, each under its own title and URL,
  with HTML comments (generator markers, test markers) removed.

Pages are listed in navigation order and every Markdown file of ``docs_dir`` must be listed: a
page the navigation leaves out raises, so the files can never miss one silently. Links are
``site_url`` + page URL when ``site_url`` is set, else site-relative.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

INDEX_FILE = "llms.txt"
FULL_FILE = "llms-full.txt"
TOP_SECTION = "Overview"

_COMMENT = re.compile(r"<!--.*?-->\n?", re.DOTALL)
_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


@dataclass(frozen=True)
class NavPage:
    section: str
    title: str
    path: str


class LlmsError(Exception):
    """The navigation and the docs folder disagree."""


def nav_pages(nav: Sequence[Any], section: str = TOP_SECTION) -> list[NavPage]:
    """The pages of a ``nav`` setting in order, each with its top-level section."""
    out: list[NavPage] = []
    for item in nav:
        if isinstance(item, str):
            out.append(NavPage(section, "", item))
            continue
        if not isinstance(item, Mapping):
            continue
        for title, value in cast(Mapping[str, Any], item).items():
            if isinstance(value, str):
                if not re.match(r"^[a-z]+://", value):
                    out.append(NavPage(section, str(title), value))
            elif isinstance(value, list):
                child = str(title) if section == TOP_SECTION else section
                out.extend(nav_pages(cast(list[Any], value), child))
    return out


def page_url(path: str, site_url: str = "", directory_urls: bool = True) -> str:
    stem = path[: -len(".md")] if path.endswith(".md") else path
    if directory_urls:
        is_index = stem == "index" or stem.endswith("/index")
        url = stem[: -len("index")] if is_index else stem + "/"
    else:
        url = stem + ".html"
    base = site_url.rstrip("/") + "/" if site_url else "/"
    return base + url


def clean(markdown: str) -> str:
    return _COMMENT.sub("", markdown).strip() + "\n"


def title_of(markdown: str, fallback: str) -> str:
    found = _HEADING.search(clean(markdown))
    return found.group(1).strip() if found else fallback


def summary_of(markdown: str) -> str:
    """The first sentence of the first plain paragraph after the title."""
    body = _HEADING.sub("", clean(markdown), count=1)
    first = ""
    for block in re.split(r"\n\s*\n", body):
        text = block.strip()
        if not text or text.startswith(("#", "```", "|", "-", "*", ">", "<", "!!!", "1.")):
            continue
        flat = _LINK.sub(r"\1", " ".join(text.split()))
        sentence = re.split(r"(?<=[.!?])\s", flat, maxsplit=1)[0]
        if sentence.endswith(":"):
            first = first or sentence
            continue  # an introduction to a list or a code block: look further
        return sentence
    return first


def missing_from_nav(docs_dir: Path, pages: Iterable[NavPage]) -> list[str]:
    listed = {p.path for p in pages}
    every = sorted(p.relative_to(docs_dir).as_posix() for p in docs_dir.rglob("*.md"))
    return [path for path in every if path not in listed]


def _sections(pages: Sequence[NavPage]) -> Iterator[tuple[str, list[NavPage]]]:
    order: list[str] = []
    grouped: dict[str, list[NavPage]] = {}
    for page in pages:
        if page.section not in grouped:
            order.append(page.section)
            grouped[page.section] = []
        grouped[page.section].append(page)
    for name in order:
        yield name, grouped[name]


def render_index(
    site_name: str,
    description: str,
    pages: Sequence[NavPage],
    docs_dir: Path,
    site_url: str = "",
    directory_urls: bool = True,
) -> str:
    lines = [f"# {site_name}", ""]
    if description:
        lines += [f"> {description}", ""]
    base = site_url.rstrip("/") + "/" if site_url else "/"
    lines += [f"Full text of every page: {base}{FULL_FILE}", ""]
    for section, members in _sections(pages):
        lines += [f"## {section}", ""]
        for page in members:
            text = (docs_dir / page.path).read_text(encoding="utf-8")
            title = title_of(text, page.title or page.path)
            url = page_url(page.path, site_url, directory_urls)
            note = summary_of(text)
            lines.append(f"- [{title}]({url})" + (f": {note}" if note else ""))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_full(
    site_name: str,
    pages: Sequence[NavPage],
    docs_dir: Path,
    site_url: str = "",
    directory_urls: bool = True,
) -> str:
    parts = [f"# {site_name}: full documentation\n"]
    for page in pages:
        text = clean((docs_dir / page.path).read_text(encoding="utf-8"))
        url = page_url(page.path, site_url, directory_urls)
        parts.append(f"<!-- page: {page.path} | {url} -->\n\n{text}")
    return "\n---\n\n".join(parts)


def write_llms_files(config: Mapping[str, Any]) -> list[Path]:
    """Write both files for an MkDocs ``config``; the paths written."""
    docs_dir = Path(str(config["docs_dir"]))
    site_dir = Path(str(config["site_dir"]))
    pages = nav_pages(cast(Sequence[Any], config.get("nav") or []))
    missing = missing_from_nav(docs_dir, pages)
    if missing:
        raise LlmsError("pages missing from the navigation: " + ", ".join(missing))
    site_url = str(config.get("site_url") or "")
    directory_urls = bool(config.get("use_directory_urls", True))
    name = str(config.get("site_name") or "")
    description = str(config.get("site_description") or "")
    index = site_dir / INDEX_FILE
    full = site_dir / FULL_FILE
    index.write_text(
        render_index(name, description, pages, docs_dir, site_url, directory_urls),
        encoding="utf-8",
    )
    full.write_text(render_full(name, pages, docs_dir, site_url, directory_urls), encoding="utf-8")
    return [index, full]


def on_post_build(config: Any, **_kwargs: Any) -> None:
    """MkDocs hook: runs once after the site is written."""
    try:
        write_llms_files(config)
    except LlmsError as exc:
        from mkdocs.exceptions import PluginError

        raise PluginError(str(exc)) from exc
