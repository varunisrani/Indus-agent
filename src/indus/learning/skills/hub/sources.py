"""Where hub skills come from (P13-25): one adapter per source behind one async interface.

Every adapter answers ``search(query, limit)``, ``resolve(identifier)`` (metadata, without the
support files) and ``fetch(identifier)`` (a ``Bundle``: ``SKILL.md`` plus the support files it
refers to). ``None`` means "not here"; a ``HubError`` carries a message the user should read (a
private repo without a token, a rate limit, an unsafe path in a bundle).

Sources:

- ``official`` — the optional catalogue that ships beside the bundled skills
  (``INDUS_OFFICIAL_SKILLS``, the packaged ``_official_skills``, else the checkout's
  ``optional-skills/``). Trust ``builtin``. Empty in 1.0 unless the owner adds skills.
- ``url`` — a direct link to a ``.md`` file. Always ``community``. The skill name is the
  frontmatter ``name`` when it looks like one, else the URL's folder slug (``…/<slug>/SKILL.md``)
  or file stem; when neither does, the bundle comes back with ``awaiting_name`` and the caller asks
  (terminal) or stops with a pointer to ``--name``.
- ``github`` — ``owner/repo/path/to/skill``. The repo's Git tree is read once per run (two API
  calls) and files come from the raw-content host pinned to the tree's sha. Taps (repos searched
  for skills under a root, default ``skills/``) come from ``.hub/taps.json``.
- ``well-known`` — a site's ``/.well-known/skills/index.json``; identifiers
  ``well-known:<base>/<name>``.
- ``skills-sh`` — the skills.sh directory: search through its API, content from GitHub; a repo's
  ``skills.sh.json`` groupings become category labels.

Only ``SKILL.md`` and files it names under ``references/``, ``templates/``, ``scripts/``,
``examples/`` and ``assets/`` are fetched; a reference that leaves the skill folder refuses the
whole bundle. Every request goes through the P05 URL policy at connect time (``safe_client``),
redirect hops included, with explicit timeouts.

Search asks every configured source within one budget; when they found nothing for a non-empty
query and no filter was given, skills.sh and the well-known sites are asked live, capped at 8 s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.scope import Scope
from indus.learning.skills.format import (
    LAYOUT_DIRS,
    SKILL_FILE,
    SkillFormatError,
    split_frontmatter,
)
from indus.learning.skills.hub.lock import Taps, Trust, safe_rel_path, safe_skill_name
from indus.safety.safe_transport import TransportFactory, safe_client
from indus.safety.url_policy import Resolver

__all__ = [
    "FALLBACK_CAP_S",
    "OFFICIAL_ENV",
    "SEARCH_BUDGET_S",
    "TRUSTED_TAPS",
    "Bundle",
    "GitHubSource",
    "HubError",
    "HubHttp",
    "OfficialSource",
    "SearchOutcome",
    "SkillMeta",
    "SkillsShSource",
    "Source",
    "SourceSet",
    "UrlSource",
    "WellKnownSource",
    "build_sources",
    "frontmatter_of",
    "official_root",
    "referenced_files",
    "search_sources",
    "url_skill_name",
]

log = logging.getLogger(__name__)

OFFICIAL_ENV = "INDUS_OFFICIAL_SKILLS"
SEARCH_BUDGET_S = 30.0
FALLBACK_CAP_S = 8.0
POLL_S = 0.05
HTTP_TIMEOUT_S = 20.0
MAX_FILE_BYTES = 1024 * 1024

# Repos whose taps may be raised to ``trusted`` (``indus skills trust <repo>``). Empty: the owner
# decides which publishers earn it.
TRUSTED_TAPS: frozenset[str] = frozenset()

_TRUST_RANK = {"builtin": 2, "trusted": 1, "community": 0}
_URL_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_RESERVED_STEMS = frozenset({"skill", "readme", "index"})
_SUPPORT = "|".join(re.escape(d) for d in LAYOUT_DIRS)
_REFERENCE = re.compile(rf"(?<![\w./-])((?:{_SUPPORT})/[^\s)`'\"<>\]|]+)")
_PLACEHOLDER_CHARS = frozenset("*?<>{}[]$")


class HubError(Exception):
    """A hub failure with a message meant for the user."""


# -- records ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillMeta:
    """One search or resolve result."""

    name: str
    description: str
    source: str
    identifier: str
    trust: Trust = "community"
    category: str = ""
    tags: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class Bundle:
    """A fetched skill waiting for quarantine: relative posix path -> bytes."""

    name: str
    files: dict[str, bytes]
    source: str
    identifier: str
    trust: Trust = "community"
    category: str = ""
    awaiting_name: bool = False
    missing: list[str] = field(default_factory=list[str])
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def skill_md(self) -> str:
        return self.files[SKILL_FILE].decode("utf-8")


def frontmatter_of(text: str) -> dict[str, Any]:
    try:
        raw, _ = split_frontmatter(text)
    except SkillFormatError:
        return {}
    return raw


def _description(raw: Mapping[str, Any]) -> str:
    value = raw.get("description")
    return " ".join(value.split()) if isinstance(value, str) else ""


def _tags(raw: Mapping[str, Any]) -> tuple[str, ...]:
    meta = raw.get("metadata")
    indus = cast(Mapping[str, Any], meta).get("indus") if isinstance(meta, Mapping) else None
    tags = cast(Mapping[str, Any], indus).get("tags") if isinstance(indus, Mapping) else None
    if tags is None:
        tags = raw.get("tags")
    return tuple(str(t) for t in cast(list[Any], tags)) if isinstance(tags, list) else ()


def _matches(query: str, *fields: object) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    hay = " ".join(" ".join(map(str, f)) if isinstance(f, tuple | list) else str(f) for f in fields)
    return needle in hay.lower()


def referenced_files(skill_md: str) -> set[str]:
    """Support files ``skill_md`` names under the layout folders.

    Raises ``HubError`` when a reference leaves the skill folder (``references/../x``): such a
    bundle is refused whole. Globs and prose placeholders (``references/<topic>.md``) are skipped.
    """
    found: set[str] = set()
    text = skill_md.replace("\\", "/")
    for match in _REFERENCE.finditer(text):
        candidate = match.group(1).rstrip(".,;:!")
        candidate = unquote(candidate.split("#", 1)[0].split("?", 1)[0])
        if not candidate or candidate.endswith("/"):
            continue
        if any(char in _PLACEHOLDER_CHARS for char in candidate):
            continue
        try:
            clean = safe_rel_path(candidate, what="support file")
        except ValueError as exc:
            raise HubError(f"SKILL.md refers to a file outside the skill folder ({exc})") from None
        if not clean[-1].isalnum() or "/" not in clean:
            continue
        found.add(clean)
    return found


def url_skill_name(frontmatter: Mapping[str, Any], url: str) -> str | None:
    """The name ladder for a URL skill: frontmatter ``name``, then the URL slug; None = ask."""

    def usable(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not _URL_NAME.match(text) or text in _RESERVED_STEMS:
            return None
        return text

    named = usable(frontmatter.get("name"))
    if named is not None:
        return named
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if not parts:
        return None
    last = unquote(parts[-1])
    if last.lower() == SKILL_FILE.lower():
        return usable(unquote(parts[-2])) if len(parts) >= 2 else None
    stem = last[:-3] if last.lower().endswith(".md") else last
    return usable(stem)


# -- HTTP ---------------------------------------------------------------------------------------


class HubHttp:
    """One SSRF-safe client for a hub run. ``get`` returns ``None`` on a refusal or failure."""

    def __init__(
        self,
        scope: Scope | None,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        inner_factory: TransportFactory | None = None,
        timeout: float = HTTP_TIMEOUT_S,
    ) -> None:
        self.client = client or safe_client(
            scope,
            resolver=resolver,
            inner_factory=inner_factory,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        )
        self.requests = 0

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response | None:
        self.requests += 1
        try:
            return await self.client.get(url, headers=dict(headers or {}), params=params)
        except httpx.HTTPError as exc:
            log.debug("hub fetch of %s failed: %s", url, exc)
            return None

    async def get_bytes(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> bytes | None:
        resp = await self.get(url, headers=headers)
        if resp is None or resp.status_code != 200:
            return None
        if len(resp.content) > MAX_FILE_BYTES:
            raise HubError(f"{url} is larger than {MAX_FILE_BYTES // 1024} KiB; not fetched")
        return resp.content

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        resp = await self.get(url, headers=headers, params=params)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> HubHttp:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


# -- the interface ------------------------------------------------------------------------------


class Source(Protocol):
    id: str

    def handles(self, identifier: str) -> bool: ...

    async def search(self, query: str, limit: int) -> list[SkillMeta]: ...

    async def resolve(self, identifier: str) -> SkillMeta | None: ...

    async def fetch(self, identifier: str) -> Bundle | None: ...


def _is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


# -- official -----------------------------------------------------------------------------------


def official_root(env: Mapping[str, str] | None = None) -> Path:
    """The optional catalogue folder (it may not exist: the catalogue is empty then)."""
    override = (env or {}).get(OFFICIAL_ENV, "").strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    packaged = here.parents[3] / "_official_skills"
    if packaged.is_dir():
        return packaged
    return here.parents[5] / "optional-skills"


class OfficialSource:
    """Skills from the optional catalogue that ships with Indus Agent (trust ``builtin``)."""

    id = "official"
    prefix = "official/"

    def __init__(self, root: Path) -> None:
        self.root = root

    def handles(self, identifier: str) -> bool:
        return identifier.startswith(self.prefix)

    def _packages(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        found: list[Path] = []
        for current, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")))
            here = Path(current)
            if SKILL_FILE in files and here != self.root:
                found.append(here)
                dirs[:] = []
        return sorted(found)

    def _meta(self, folder: Path) -> SkillMeta | None:
        try:
            raw = frontmatter_of((folder / SKILL_FILE).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return None
        rel = folder.relative_to(self.root).as_posix()
        parent = PurePosixPath(rel).parent.as_posix()
        return SkillMeta(
            name=str(raw.get("name") or folder.name),
            description=_description(raw),
            source=self.id,
            identifier=f"{self.prefix}{rel}",
            trust="builtin",
            category="" if parent == "." else parent,
            tags=_tags(raw),
        )

    async def search(self, query: str, limit: int) -> list[SkillMeta]:
        rows: list[SkillMeta] = []
        for folder in self._packages():
            meta = self._meta(folder)
            if meta is not None and _matches(query, meta.name, meta.description, meta.tags):
                rows.append(meta)
            if len(rows) >= limit:
                break
        return rows

    def _folder(self, identifier: str) -> Path | None:
        try:
            rel = safe_rel_path(identifier.removeprefix(self.prefix), what="identifier")
        except ValueError:
            return None
        folder = self.root / rel
        try:
            if not folder.resolve().is_relative_to(self.root.resolve()):
                return None
        except OSError:
            return None
        return folder if (folder / SKILL_FILE).is_file() else None

    async def resolve(self, identifier: str) -> SkillMeta | None:
        folder = self._folder(identifier)
        return self._meta(folder) if folder is not None else None

    async def fetch(self, identifier: str) -> Bundle | None:
        folder = self._folder(identifier)
        meta = self._meta(folder) if folder is not None else None
        if folder is None or meta is None:
            return None
        files: dict[str, bytes] = {}
        for current, dirs, names in os.walk(folder, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for name in names:
                path = Path(current) / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                files[path.relative_to(folder).as_posix()] = path.read_bytes()
        return Bundle(
            name=folder.name,
            files=files,
            source=self.id,
            identifier=meta.identifier,
            trust="builtin",
            category=meta.category,
        )


# -- direct URL ---------------------------------------------------------------------------------


class UrlSource:
    """A direct link to a skill's ``.md`` file; always community trust."""

    id = "url"

    def __init__(self, http: HubHttp) -> None:
        self.http = http

    def handles(self, identifier: str) -> bool:
        text = identifier.strip()
        if not _is_url(text) or "/.well-known/skills/" in text:
            return False
        return urlsplit(text).path.lower().endswith(".md")

    async def search(self, query: str, limit: int) -> list[SkillMeta]:
        return []

    async def _load(self, identifier: str) -> tuple[str, str, dict[str, Any]] | None:
        if not self.handles(identifier):
            return None
        url = identifier.strip()
        data = await self.http.get_bytes(url)
        if data is None:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HubError(f"{url} is not UTF-8 text") from None
        return url, text, frontmatter_of(text)

    async def resolve(self, identifier: str) -> SkillMeta | None:
        loaded = await self._load(identifier)
        if loaded is None:
            return None
        url, _text, raw = loaded
        name = url_skill_name(raw, url)
        return SkillMeta(
            name=name or "",
            description=_description(raw),
            source=self.id,
            identifier=url,
            tags=_tags(raw),
            extra={"awaiting_name": name is None},
        )

    async def fetch(self, identifier: str) -> Bundle | None:
        loaded = await self._load(identifier)
        if loaded is None:
            return None
        url, text, raw = loaded
        files: dict[str, bytes] = {SKILL_FILE: text.encode("utf-8")}
        missing: list[str] = []
        base = url.rsplit("/", 1)[0] + "/"
        host = urlsplit(url).netloc
        for rel in sorted(referenced_files(text)):
            target = urljoin(base, quote(rel, safe="/"))
            if urlsplit(target).netloc != host:
                raise HubError(f"support file {rel!r} would be fetched from another host")
            data = await self.http.get_bytes(target)
            if data is None:
                missing.append(rel)
                continue
            files[rel] = data
        name = url_skill_name(raw, url)
        return Bundle(
            name=name or "",
            files=files,
            source=self.id,
            identifier=url,
            awaiting_name=name is None,
            missing=missing,
            metadata={"url": url},
        )


# -- GitHub -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tree:
    branch: str
    sha: str
    entries: Mapping[str, Mapping[str, Any]]
    """Path -> tree entry (``type``, ``mode``)."""

    def blob(self, path: str) -> Mapping[str, Any] | None:
        entry = self.entries.get(path)
        return entry if entry is not None and entry.get("type") == "blob" else None

    @staticmethod
    def is_link(entry: Mapping[str, Any]) -> bool:
        return str(entry.get("mode", "")) == "120000"


def _split_repo(identifier: str) -> tuple[str, str] | None:
    parts = identifier.strip().strip("/").split("/", 2)
    if len(parts) < 2 or not all(parts[:2]):
        return None
    return f"{parts[0]}/{parts[1]}", (parts[2] if len(parts) == 3 else "")


class GitHubSource:
    """Skills in GitHub repos, searched through taps; files from the raw host at a pinned sha."""

    id = "github"
    prefix = "github/"

    def __init__(
        self,
        http: HubHttp,
        *,
        taps: Sequence[Mapping[str, str]] = (),
        token: str | None = None,
        api: str = "https://api.github.com",
        raw: str = "https://raw.githubusercontent.com",
        trusted: frozenset[str] = TRUSTED_TAPS,
    ) -> None:
        self.http = http
        self.taps = [dict(t) for t in taps]
        self.token = token or None
        self.api = api.rstrip("/")
        self.raw = raw.rstrip("/")
        self.trusted = trusted
        self._trees: dict[str, _Tree] = {}
        self._texts: dict[tuple[str, str], str | None] = {}
        self._groupings: dict[str, dict[str, str]] = {}

    def handles(self, identifier: str) -> bool:
        text = identifier.strip()
        if _is_url(text) or ":" in text.split("/", 1)[0]:
            return False
        return _split_repo(text.removeprefix(self.prefix)) is not None and text.count("/") >= 2

    def trust_for(self, repo: str) -> Trust:
        key = repo.lower()
        if key not in {t.lower() for t in self.trusted}:
            return "community"
        for tap in self.taps:
            if tap.get("repo", "").lower() == key and tap.get("trust") == "trusted":
                return "trusted"
        return "community"

    def _headers(self, *, api: bool) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"} if api else {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _api(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        url = f"{self.api}{path}"
        resp = await self.http.get(url, headers=self._headers(api=True), params=params)
        if resp is None:
            raise HubError("GitHub could not be reached")
        if resp.status_code in (403, 429) and (
            resp.status_code == 429 or resp.headers.get("x-ratelimit-remaining") == "0"
        ):
            hint = "" if self.token else " Set GITHUB_TOKEN to raise the limit to 5,000 an hour."
            raise HubError(f"GitHub's API rate limit is used up.{hint}")
        if resp.status_code != 200:
            return resp.status_code
        try:
            return resp.json()
        except ValueError:
            return None

    async def tree(self, repo: str) -> _Tree:
        cached = self._trees.get(repo)
        if cached is not None:
            return cached
        info = await self._api(f"/repos/{repo}")
        if info == 404:
            if self.token:
                raise HubError(f"GitHub repo {repo!r} was not found (or the token cannot see it)")
            raise HubError(
                f"GitHub repo {repo!r} was not found, or it is private: set GITHUB_TOKEN (or "
                "GH_TOKEN) to a token that can read it"
            )
        if not isinstance(info, dict):
            raise HubError(f"GitHub did not describe {repo!r}")
        branch = str(cast(dict[str, Any], info).get("default_branch") or "main")
        listing = await self._api(f"/repos/{repo}/git/trees/{quote(branch)}", {"recursive": "1"})
        if not isinstance(listing, dict):
            raise HubError(f"GitHub did not list the files of {repo!r}")
        data = cast(dict[str, Any], listing)
        if data.get("truncated"):
            raise HubError(f"{repo!r} is too large for one tree listing; install by URL instead")
        entries: dict[str, Mapping[str, Any]] = {}
        for item in cast(list[Any], data.get("tree") or []):
            if isinstance(item, dict) and isinstance(item.get("path"), str):  # pyright: ignore[reportUnknownMemberType]
                entries[str(item["path"])] = cast(dict[str, Any], item)  # pyright: ignore[reportUnknownArgumentType]
        tree = _Tree(branch=branch, sha=str(data.get("sha") or branch), entries=entries)
        self._trees[repo] = tree
        return tree

    async def _file(self, repo: str, tree: _Tree, path: str) -> bytes | None:
        url = f"{self.raw}/{repo}/{tree.sha}/{quote(path, safe='/')}"
        return await self.http.get_bytes(url, headers=self._headers(api=False))

    async def _text(self, repo: str, tree: _Tree, path: str) -> str | None:
        key = (repo, path)
        if key not in self._texts:
            data = await self._file(repo, tree, path)
            try:
                self._texts[key] = data.decode("utf-8") if data is not None else None
            except UnicodeDecodeError:
                self._texts[key] = None
        return self._texts[key]

    async def groupings(self, repo: str) -> dict[str, str]:
        """``skills.sh.json`` at the repo root as ``{skill: category label}`` (first group wins)."""
        if repo in self._groupings:
            return self._groupings[repo]
        mapping: dict[str, str] = {}
        tree = await self.tree(repo)
        text = None
        if tree.blob("skills.sh.json") is not None:
            text = await self._text(repo, tree, "skills.sh.json")
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        groups = cast(dict[str, Any], data).get("groupings") if isinstance(data, dict) else None
        for group in cast(list[Any], groups) if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            item = cast(dict[str, Any], group)
            title, members = item.get("title"), item.get("skills")
            if not isinstance(title, str) or not isinstance(members, list):
                continue
            for member in cast(list[Any], members):
                if isinstance(member, str) and member:
                    mapping.setdefault(member, title)
        self._groupings[repo] = mapping
        return mapping

    def skill_dirs(self, tree: _Tree, root: str) -> list[str]:
        """Folders directly under ``root`` holding a ``SKILL.md`` (``.``/``_`` folders skipped)."""
        prefix = root.strip("/")
        prefix = f"{prefix}/" if prefix else ""
        found: list[str] = []
        for path, entry in tree.entries.items():
            if not path.startswith(prefix) or not path.endswith(f"/{SKILL_FILE}"):
                continue
            middle = path[len(prefix) : -len(SKILL_FILE) - 1]
            if not middle or "/" in middle or middle.startswith((".", "_")):
                continue
            if entry.get("type") == "blob" and not _Tree.is_link(entry):
                found.append(f"{prefix}{middle}")
        return sorted(found)

    async def _meta(self, repo: str, tree: _Tree, skill_dir: str) -> SkillMeta | None:
        path = f"{skill_dir}/{SKILL_FILE}" if skill_dir else SKILL_FILE
        text = await self._text(repo, tree, path)
        if text is None:
            return None
        raw = frontmatter_of(text)
        folder = skill_dir.rsplit("/", 1)[-1] if skill_dir else repo.split("/")[-1]
        name = str(raw.get("name") or folder)
        groups = await self.groupings(repo)
        return SkillMeta(
            name=name,
            description=_description(raw),
            source=self.id,
            identifier=f"{repo}/{skill_dir}" if skill_dir else f"{repo}/",
            trust=self.trust_for(repo),
            category=groups.get(name) or groups.get(folder) or "",
            tags=_tags(raw),
            extra={"repo": repo, "path": skill_dir},
        )

    async def list_tap(self, repo: str, root: str = "skills/") -> list[SkillMeta]:
        tree = await self.tree(repo)
        rows: list[SkillMeta] = []
        for skill_dir in self.skill_dirs(tree, root):
            meta = await self._meta(repo, tree, skill_dir)
            if meta is not None:
                rows.append(meta)
        return rows

    async def search(self, query: str, limit: int, *, tap: str | None = None) -> list[SkillMeta]:
        rows: list[SkillMeta] = []
        for entry in self.taps:
            repo = entry.get("repo", "")
            if tap is not None and repo.lower() != tap.lower():
                continue
            try:
                listed = await self.list_tap(repo, entry.get("path") or "skills/")
            except HubError as exc:
                log.info("tap %s skipped: %s", repo, exc)
                continue
            rows.extend(m for m in listed if _matches(query, m.name, m.description, m.tags))
            if len(rows) >= limit:
                break
        return rows[:limit]

    def _parts(self, identifier: str) -> tuple[str, str]:
        split = _split_repo(identifier.strip().removeprefix(self.prefix))
        if split is None:
            raise HubError(f"{identifier!r} is not owner/repo/path")
        repo, path = split
        try:
            path = safe_rel_path(path, what="skill path") if path.strip("/") else ""
        except ValueError as exc:
            raise HubError(str(exc)) from None
        return repo, path

    async def resolve(self, identifier: str) -> SkillMeta | None:
        repo, path = self._parts(identifier)
        tree = await self.tree(repo)
        if tree.blob(f"{path}/{SKILL_FILE}" if path else SKILL_FILE) is None:
            return None
        return await self._meta(repo, tree, path)

    async def fetch(self, identifier: str) -> Bundle | None:
        repo, path = self._parts(identifier)
        tree = await self.tree(repo)
        skill_path = f"{path}/{SKILL_FILE}" if path else SKILL_FILE
        entry = tree.blob(skill_path)
        if entry is None:
            return None
        if _Tree.is_link(entry):
            raise HubError(f"{skill_path} in {repo} is a symlink; refused")
        text = await self._text(repo, tree, skill_path)
        if text is None:
            raise HubError(f"could not download {skill_path} from {repo}")
        files: dict[str, bytes] = {SKILL_FILE: text.encode("utf-8")}
        missing: list[str] = []
        for rel in sorted(referenced_files(text)):
            full = f"{path}/{rel}" if path else rel
            member = tree.blob(full)
            if member is None:
                missing.append(rel)
                continue
            if _Tree.is_link(member):
                raise HubError(f"{rel} in {repo} is a symlink; refused")
            data = await self._file(repo, tree, full)
            if data is None:
                missing.append(rel)
                continue
            files[rel] = data
        meta = await self._meta(repo, tree, path)
        folder = path.rsplit("/", 1)[-1] if path else repo.split("/")[-1]
        return Bundle(
            name=folder,
            files=files,
            source=self.id,
            identifier=f"{repo}/{path}" if path else f"{repo}/",
            trust=self.trust_for(repo),
            category=meta.category if meta is not None else "",
            missing=missing,
            metadata={"repo": repo, "revision": tree.sha},
        )

    async def find_skill(self, repo: str, slug: str) -> str | None:
        """The identifier of ``<anything>/<slug>/SKILL.md`` in ``repo`` (a root skill counts)."""
        tree = await self.tree(repo)
        hits = sorted(
            p[: -len(SKILL_FILE) - 1]
            for p, e in tree.entries.items()
            if p.endswith(f"{slug}/{SKILL_FILE}")
            and (p == f"{slug}/{SKILL_FILE}" or p.endswith(f"/{slug}/{SKILL_FILE}"))
            and e.get("type") == "blob"
            and not _Tree.is_link(e)
        )
        if hits:
            return f"{repo}/{hits[0]}"
        roots = [p for p, e in tree.entries.items() if p == SKILL_FILE and e.get("type") == "blob"]
        return f"{repo}/" if roots else None


# -- well-known ---------------------------------------------------------------------------------


WELL_KNOWN = "/.well-known/skills"


class WellKnownSource:
    """``https://<site>/.well-known/skills/index.json`` publishers (community trust)."""

    id = "well-known"
    prefix = "well-known:"

    def __init__(self, http: HubHttp, sites: Sequence[str] = ()) -> None:
        self.http = http
        self.sites = [s.rstrip("/") for s in sites if _is_url(s)]
        self._indexes: dict[str, list[dict[str, Any]] | None] = {}

    def handles(self, identifier: str) -> bool:
        text = identifier.strip()
        return text.startswith(self.prefix) or (_is_url(text) and f"{WELL_KNOWN}/" in text)

    @staticmethod
    def base_of(site_or_url: str) -> str:
        text = site_or_url.strip().split("#", 1)[0]
        if f"{WELL_KNOWN}/" in text or text.rstrip("/").endswith(WELL_KNOWN):
            return text.split(WELL_KNOWN, 1)[0] + WELL_KNOWN
        return text.rstrip("/") + WELL_KNOWN

    async def index(self, base: str) -> list[dict[str, Any]] | None:
        if base not in self._indexes:
            data = await self.http.get_json(f"{base}/index.json")
            skills = cast(dict[str, Any], data).get("skills") if isinstance(data, dict) else None
            rows: list[dict[str, Any]] | None = None
            if isinstance(skills, list):
                rows = []
                for item in cast(list[Any], skills):
                    if isinstance(item, dict) and isinstance(item.get("name"), str):  # pyright: ignore[reportUnknownMemberType]
                        rows.append(cast(dict[str, Any], item))
            self._indexes[base] = rows
        return self._indexes[base]

    def _meta(self, base: str, item: Mapping[str, Any]) -> SkillMeta:
        name = str(item["name"])
        return SkillMeta(
            name=name,
            description=" ".join(str(item.get("description") or "").split()),
            source=self.id,
            identifier=f"{self.prefix}{base}/{name}",
            extra={"index": f"{base}/index.json"},
        )

    async def search(self, query: str, limit: int) -> list[SkillMeta]:
        if _is_url(query):
            bases, needle = [self.base_of(query)], ""
        else:
            bases, needle = [self.base_of(s) for s in self.sites], query
        rows: list[SkillMeta] = []
        for base in bases:
            for item in await self.index(base) or []:
                meta = self._meta(base, item)
                if _matches(needle, meta.name, meta.description):
                    rows.append(meta)
        return rows[:limit]

    def _split(self, identifier: str) -> tuple[str, str]:
        text = identifier.strip().removeprefix(self.prefix).split("#", 1)[0]
        text = text.removesuffix(f"/{SKILL_FILE}").rstrip("/")
        if f"{WELL_KNOWN}/" not in f"{text}/" or not _is_url(text):
            raise HubError(f"{identifier!r} is not a well-known skill address")
        base, name = text.rsplit("/", 1)
        if not base.endswith(WELL_KNOWN):
            raise HubError(f"{identifier!r} is not directly under {WELL_KNOWN}/")
        try:
            return base, safe_skill_name(name)
        except ValueError as exc:
            raise HubError(str(exc)) from None

    async def _entry(self, identifier: str) -> tuple[str, Mapping[str, Any]] | None:
        base, name = self._split(identifier)
        for item in await self.index(base) or []:
            if item.get("name") == name:
                return base, item
        return None

    async def resolve(self, identifier: str) -> SkillMeta | None:
        found = await self._entry(identifier)
        return self._meta(*found) if found is not None else None

    async def fetch(self, identifier: str) -> Bundle | None:
        found = await self._entry(identifier)
        if found is None:
            return None
        base, item = found
        name = str(item["name"])
        listed = item.get("files")
        wanted = cast(list[Any], listed) if isinstance(listed, list) and listed else [SKILL_FILE]
        files: dict[str, bytes] = {}
        missing: list[str] = []
        for raw in wanted:
            try:
                rel = safe_rel_path(str(raw), what="index file path")
            except ValueError as exc:
                raise HubError(f"{name}: the index lists an unsafe file ({exc})") from None
            if rel != SKILL_FILE and rel.split("/", 1)[0] not in LAYOUT_DIRS:
                continue
            data = await self.http.get_bytes(f"{base}/{name}/{quote(rel, safe='/')}")
            if data is None:
                if rel == SKILL_FILE:
                    return None
                missing.append(rel)
                continue
            files[rel] = data
        if SKILL_FILE not in files:
            return None
        return Bundle(
            name=name,
            files=files,
            source=self.id,
            identifier=f"{self.prefix}{base}/{name}",
            missing=missing,
            metadata={"index": f"{base}/index.json"},
        )


# -- skills.sh ----------------------------------------------------------------------------------


class SkillsShSource:
    """The skills.sh directory: search through its API, content from the GitHub repo it names."""

    id = "skills-sh"
    prefixes = ("skills-sh/", "skills.sh/")
    candidate_roots = ("", "skills/", ".agents/skills/", ".claude/skills/")

    def __init__(self, http: HubHttp, github: GitHubSource, base: str = "https://skills.sh"):
        self.http = http
        self.github = github
        self.base = base.rstrip("/")

    def handles(self, identifier: str) -> bool:
        return identifier.strip().startswith(self.prefixes)

    def canonical(self, identifier: str) -> str:
        text = identifier.strip()
        for prefix in self.prefixes:
            text = text.removeprefix(prefix)
        return text.strip("/")

    async def search(self, query: str, limit: int) -> list[SkillMeta]:
        if not query.strip():
            return []
        data = await self.http.get_json(
            f"{self.base}/api/search", params={"q": query, "limit": str(limit)}
        )
        items = cast(dict[str, Any], data).get("skills") if isinstance(data, dict) else None
        rows: list[SkillMeta] = []
        for item in cast(list[Any], items) if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            row = cast(dict[str, Any], item)
            ident = row.get("id")
            if not isinstance(ident, str) or ident.count("/") < 2:
                source, skill = row.get("source"), row.get("skillId")
                if not (isinstance(source, str) and isinstance(skill, str)):
                    continue
                ident = f"{source}/{skill}"
            split = _split_repo(ident)
            if split is None:
                continue
            repo, path = split
            installs = row.get("installs")
            note = f" · {installs:,} installs" if isinstance(installs, int) else ""
            rows.append(
                SkillMeta(
                    name=str(row.get("name") or path.rsplit("/", 1)[-1]),
                    description=f"Listed on skills.sh from {repo}{note}",
                    source=self.id,
                    identifier=f"{self.prefixes[0]}{ident}",
                    trust=self.github.trust_for(repo),
                    extra={"repo": repo, "installs": installs},
                )
            )
            if len(rows) >= limit:
                break
        return rows

    async def _github_id(self, identifier: str) -> str | None:
        canonical = self.canonical(identifier)
        split = _split_repo(canonical)
        if split is None or not split[1]:
            raise HubError(f"{identifier!r} is not skills-sh/owner/repo/skill")
        repo, path = split
        tree = await self.github.tree(repo)
        for root in self.candidate_roots:
            if tree.blob(f"{root}{path}/{SKILL_FILE}") is not None:
                return f"{repo}/{root}{path}"
        return await self.github.find_skill(repo, path.rsplit("/", 1)[-1])

    def _relabel(self, identifier: str, name: str) -> tuple[str, str]:
        canonical = self.canonical(identifier)
        slug = canonical.rsplit("/", 1)[-1]
        return f"{self.prefixes[0]}{canonical}", (slug if name in ("", "skills") else name)

    async def resolve(self, identifier: str) -> SkillMeta | None:
        found = await self._github_id(identifier)
        meta = await self.github.resolve(found) if found else None
        if meta is None:
            return None
        ident, name = self._relabel(identifier, meta.name)
        return replace(meta, source=self.id, identifier=ident, name=name)

    async def fetch(self, identifier: str) -> Bundle | None:
        found = await self._github_id(identifier)
        bundle = await self.github.fetch(found) if found else None
        if bundle is None:
            return None
        bundle.identifier, bundle.name = self._relabel(identifier, bundle.name)
        bundle.source = self.id
        return bundle


# -- the set of sources -------------------------------------------------------------------------


@dataclass
class SourceSet:
    """Every adapter of one hub run, sharing one HTTP client."""

    http: HubHttp
    official: OfficialSource
    url: UrlSource
    github: GitHubSource
    well_known: WellKnownSource
    skills_sh: SkillsShSource

    @property
    def all(self) -> list[Source]:
        return [self.official, self.url, self.github, self.well_known, self.skills_sh]

    @property
    def configured(self) -> list[Source]:
        """Searched first: the catalogue and the taps."""
        return [self.official, self.github]

    @property
    def fallback(self) -> list[Source]:
        """Asked live when the configured sources found nothing: skills.sh and the well-known
        sites of ``skills.hub.well_known``."""
        return [self.skills_sh, self.well_known] if self.well_known.sites else [self.skills_sh]

    def by_id(self, source_id: str) -> Source | None:
        wanted = "skills-sh" if source_id == "skills.sh" else source_id
        return next((s for s in self.all if s.id == wanted), None)

    def for_identifier(self, identifier: str) -> Source | None:
        """The adapter an identifier names by its shape; None for a bare name."""
        for source in (self.official, self.well_known, self.url, self.skills_sh, self.github):
            if source.handles(identifier):
                return source
        return None

    async def aclose(self) -> None:
        await self.http.aclose()


def _setting(scope: Scope, key: str, default: Any) -> Any:
    from indus.learning.skills.resolver import skills_setting

    value = skills_setting(scope, key)
    return default if value is None else value


def github_token(scope: Scope) -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = scope.secrets.get(name) if scope.secrets is not None else None
        value = value or scope.env.get(name)
        if value:
            return value
    return None


def build_sources(
    scope: Scope,
    *,
    http: HubHttp | None = None,
    official: Path | None = None,
    github_api: str = "https://api.github.com",
    github_raw: str = "https://raw.githubusercontent.com",
    skills_sh: str = "https://skills.sh",
) -> SourceSet:
    """The adapters for ``scope``: taps from ``.hub/taps.json``, sites from ``skills.hub``."""
    client = http or HubHttp(scope)
    taps = Taps(scope.home / "skills").list()
    sites_raw = _setting(scope, "skills.hub.well_known", [])
    sites = [str(s) for s in cast(list[Any], sites_raw)] if isinstance(sites_raw, list) else []
    github = GitHubSource(
        client, taps=taps, token=github_token(scope), api=github_api, raw=github_raw
    )
    return SourceSet(
        http=client,
        official=OfficialSource(official or official_root(dict(scope.env))),
        url=UrlSource(client),
        github=github,
        well_known=WellKnownSource(client, sites),
        skills_sh=SkillsShSource(client, github, skills_sh),
    )


# -- search -------------------------------------------------------------------------------------


@dataclass
class SearchOutcome:
    rows: list[SkillMeta]
    counts: dict[str, int] = field(default_factory=dict[str, int])
    timed_out: list[str] = field(default_factory=list[str])
    fallback_used: bool = False


type _Search = Callable[[Source], Any]


async def _run_until(
    sources: Iterable[Source],
    call: _Search,
    deadline: float,
    clock: Clock,
    outcome: SearchOutcome,
) -> None:
    tasks: dict[asyncio.Task[Any], str] = {
        asyncio.ensure_future(call(src)): src.id for src in sources
    }
    pending = set(tasks)
    while pending:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=min(POLL_S, remaining), return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            sid = tasks[task]
            try:
                found = cast(list[SkillMeta], task.result())
            except Exception as exc:
                log.info("search in %s failed: %s", sid, exc)
                found = []
            outcome.counts[sid] = outcome.counts.get(sid, 0) + len(found)
            outcome.rows.extend(found)
    for task in pending:
        outcome.timed_out.append(tasks[task])
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _dedupe(rows: Iterable[SkillMeta]) -> list[SkillMeta]:
    kept: dict[str, SkillMeta] = {}
    for row in rows:
        old = kept.get(row.identifier)
        if old is None or _TRUST_RANK.get(row.trust, 0) > _TRUST_RANK.get(old.trust, 0):
            kept[row.identifier] = row
    ordered = list(kept.values())
    ordered.sort(key=lambda r: -_TRUST_RANK.get(r.trust, 0))
    return ordered


async def search_sources(
    sources: SourceSet,
    query: str,
    *,
    limit: int = 20,
    source_filter: str = "all",
    budget: float = SEARCH_BUDGET_S,
    clock: Clock | None = None,
) -> SearchOutcome:
    """Search the configured sources, then (only when they found nothing for a real query and no
    filter is set) skills.sh and the well-known sites, capped at ``FALLBACK_CAP_S``.

    ``source_filter``: ``all``, a source id, or a tap repo (``owner/repo``) to search only that tap.
    """
    clock = clock or SystemClock()
    deadline = clock.monotonic() + budget
    outcome = SearchOutcome(rows=[])
    filtered = source_filter not in ("", "all")
    tap: str | None = None
    if not filtered:
        active = sources.configured
    elif (only := sources.by_id(source_filter)) is not None:
        active = [only] if only is sources.official else [sources.official, only]
    elif "/" in source_filter:
        tap, active = source_filter, [sources.github]
    else:
        raise HubError(f"unknown source {source_filter!r}")

    def call(src: Source) -> Any:
        if tap is not None and src is sources.github:
            return sources.github.search(query, limit, tap=tap)
        return src.search(query, limit)

    await _run_until(active, call, deadline, clock, outcome)
    if not filtered and query.strip() and not outcome.rows:
        outcome.fallback_used = True
        cap = min(deadline, clock.monotonic() + FALLBACK_CAP_S)
        await _run_until(sources.fallback, lambda s: s.search(query, limit), cap, clock, outcome)
    outcome.rows = _dedupe(outcome.rows)[:limit]
    return outcome


async def find_by_name(sources: SourceSet, name: str, *, clock: Clock | None = None) -> str:
    """The one identifier whose skill is called ``name`` (bare-name installs)."""
    outcome = await search_sources(sources, name, limit=50, clock=clock)
    exact = [r for r in outcome.rows if r.name == name]
    if not exact:
        raise HubError(
            f"no hub skill is called {name!r}; `indus skills search {name}` lists close matches"
        )
    best = _TRUST_RANK.get(exact[0].trust, 0)
    top = [r for r in exact if _TRUST_RANK.get(r.trust, 0) == best]
    if len(top) > 1:
        listed = ", ".join(r.identifier for r in top)
        raise HubError(f"several skills are called {name!r}; install one by identifier: {listed}")
    return top[0].identifier
