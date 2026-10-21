"""More hub sources (P24-45): ClawHub, LobeHub and browse.sh behind the P13 ``Source`` interface.

Every skill from these sources is ``community`` trust and goes through the normal install
pipeline (quarantine, format check, scan, trust policy): their content is untrusted (R07.26).

- ``clawhub/<slug>`` or ``clawhub/@owner/<slug>`` — the ClawHub HTTP API (v1). Search by the
  listing endpoint; details by slug (with ``owner`` when given; an answer for another owner is
  ignored); content is the latest version's zip (download ≤ 25 MiB, members checked with the hub's
  safe-path rule, links skipped, only ``SKILL.md`` and the support folders kept, ≤ 1 MiB each),
  else the version record's inline files.
- ``lobehub/<agent>`` — agent entries of the LobeHub index; a fetch turns the agent (title,
  description, tags, system role) into a ``SKILL.md`` of our layout.
- ``browse-sh/<host>/<task>`` — site-automation skills from the browse.sh catalogue; the content
  always comes from the per-skill detail endpoint's ``skillMdUrl``.

Catalogues (LobeHub index, browse.sh list) are read once per hub run.
"""

from __future__ import annotations

import io
import logging
import re
import stat
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import quote, urlsplit

from indus.foundation import yamlio
from indus.learning.skills.format import LAYOUT_DIRS, SKILL_FILE
from indus.learning.skills.hub.lock import safe_rel_path, safe_skill_name
from indus.learning.skills.hub.sources import (
    MAX_FILE_BYTES,
    Bundle,
    HubError,
    HubHttp,
    SkillMeta,
)

__all__ = [
    "BROWSE_SH_BASE",
    "CLAWHUB_BASE",
    "LOBEHUB_BASE",
    "MAX_ZIP_BYTES",
    "BrowseShSource",
    "ClawHubSource",
    "LobeHubSource",
    "lobehub_skill_md",
]

log = logging.getLogger(__name__)

CLAWHUB_BASE = "https://clawhub.ai/api/v1"
LOBEHUB_BASE = "https://chat-agents.lobehub.com"
BROWSE_SH_BASE = "https://browse.sh"
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_ZIP_MEMBERS = 500
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


def _matches(query: str, *fields: object) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    parts: list[str] = []
    for value in fields:
        if isinstance(value, list | tuple):
            parts.extend(str(v) for v in cast(Sequence[object], value))
        elif value is not None:
            parts.append(str(value))
    return needle in " ".join(parts).lower()


def _text(value: object, limit: int = 1024) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tags(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(t) for t in cast(list[object], value) if str(t).strip())
    if isinstance(value, Mapping):
        return tuple(str(k) for k in cast(Mapping[object, object], value) if str(k) != "latest")
    return ()


def _kept(rel: str) -> bool:
    return rel == SKILL_FILE or (rel.split("/", 1)[0] in LAYOUT_DIRS and "/" in rel)


# -- ClawHub --------------------------------------------------------------------------------------


class ClawHubSource:
    """The ClawHub registry (see the module notes)."""

    id = "clawhub"
    prefix = "clawhub/"

    def __init__(self, http: HubHttp, base: str = CLAWHUB_BASE) -> None:
        self.http = http
        self.base = base.rstrip("/")

    def handles(self, identifier: str) -> bool:
        return identifier.strip().startswith(self.prefix)

    @classmethod
    def parse(cls, identifier: str) -> tuple[str, str | None] | None:
        """``(slug, owner)`` for ``clawhub/<slug>`` or ``clawhub/@owner/<slug>``."""
        text = identifier.strip().removeprefix(cls.prefix).strip("/")
        owner: str | None = None
        if text.startswith("@"):
            owner, _, text = text[1:].partition("/")
            if not owner or not _SLUG.match(owner):
                return None
        if not text or "/" in text or not _SLUG.match(text):
            return None
        return text, owner

    def _ident(self, slug: str, owner: str | None) -> str:
        return f"{self.prefix}@{owner}/{slug}" if owner else f"{self.prefix}{slug}"

    @staticmethod
    def _owner_of(item: Mapping[str, Any]) -> str | None:
        owner = item.get("owner")
        if isinstance(owner, Mapping):
            owner = cast(Mapping[str, Any], owner).get("handle")
        return owner.strip() if isinstance(owner, str) and owner.strip() else None

    @staticmethod
    def _flatten(data: object) -> dict[str, Any] | None:
        """A detail or listing payload with its nested ``skill`` lifted to the top."""
        if not isinstance(data, Mapping):
            return None
        raw = cast(Mapping[str, Any], data)
        nested = raw.get("skill")
        if not isinstance(nested, Mapping):
            return dict(raw)
        merged = dict(cast(Mapping[str, Any], nested))
        for key in ("latestVersion", "owner"):
            if key in raw and key not in merged:
                merged[key] = raw[key]
        return merged

    def _meta(self, item: Mapping[str, Any], owner: str | None = None) -> SkillMeta | None:
        slug = item.get("slug")
        if not isinstance(slug, str) or not _SLUG.match(slug):
            return None
        found_owner = owner or self._owner_of(item)
        name = item.get("displayName") or item.get("name") or slug
        return SkillMeta(
            name=str(name),
            description=_text(item.get("summary") or item.get("description")),
            source=self.id,
            identifier=self._ident(slug, found_owner),
            trust="community",
            tags=_tags(item.get("tags")),
            extra={"owner": found_owner} if found_owner else {},
        )

    async def search(self, query: str, limit: int) -> list[SkillMeta]:
        if not query.strip():
            return []
        data = await self.http.get_json(
            f"{self.base}/skills", params={"search": query, "limit": str(limit)}
        )
        items = cast(Mapping[str, Any], data).get("items") if isinstance(data, Mapping) else data
        rows: list[SkillMeta] = []
        for item in cast(list[object], items) if isinstance(items, list) else []:
            flat = self._flatten(item)
            meta = self._meta(flat) if flat is not None else None
            if meta is not None:
                rows.append(meta)
            if len(rows) >= limit:
                break
        return rows

    async def _detail(self, identifier: str) -> tuple[str, str | None, dict[str, Any]] | None:
        parsed = self.parse(identifier)
        if parsed is None:
            raise HubError(f"{identifier!r} is not clawhub/<slug> or clawhub/@owner/<slug>")
        slug, owner = parsed
        params = {"owner": owner} if owner else None
        data = self._flatten(
            await self.http.get_json(f"{self.base}/skills/{quote(slug)}", params=params)
        )
        if data is None:
            return None
        actual = self._owner_of(data)
        if owner and actual and actual.lower() != owner.lower():
            log.info("clawhub answered %s for another owner; ignored", slug)
            return None
        return slug, owner or actual, data

    async def resolve(self, identifier: str) -> SkillMeta | None:
        found = await self._detail(identifier)
        if found is None:
            return None
        slug, owner, data = found
        return self._meta({**data, "slug": data.get("slug") or slug}, owner)

    async def _version(self, slug: str, owner: str | None, data: Mapping[str, Any]) -> str | None:
        latest = data.get("latestVersion")
        if isinstance(latest, Mapping):
            value = cast(Mapping[str, Any], latest).get("version")
            if isinstance(value, str) and value:
                return value
        if isinstance(latest, str) and latest:
            return latest
        tags = data.get("tags")
        if isinstance(tags, Mapping):
            value = cast(Mapping[str, Any], tags).get("latest")
            if isinstance(value, str) and value:
                return value
        params = {"owner": owner} if owner else None
        listed = await self.http.get_json(
            f"{self.base}/skills/{quote(slug)}/versions", params=params
        )
        if isinstance(listed, Mapping):
            listed = cast(Mapping[str, Any], listed).get("items")
        if isinstance(listed, list) and listed and isinstance(listed[0], Mapping):
            value = cast(Mapping[str, Any], listed[0]).get("version")
            return value if isinstance(value, str) and value else None
        return None

    async def _download(self, slug: str, version: str, owner: str | None) -> bytes | None:
        params = {"slug": slug, "version": version, **({"owner": owner} if owner else {})}
        self.http.requests += 1
        data = bytearray()
        try:
            async with self.http.client.stream(
                "GET", f"{self.base}/download", params=params
            ) as resp:
                if resp.status_code != 200:
                    return None
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_ZIP_BYTES:
                    raise HubError(f"the ClawHub archive of {slug} is larger than 25 MiB")
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_ZIP_BYTES:
                        raise HubError(f"the ClawHub archive of {slug} is larger than 25 MiB")
        except HubError:
            raise
        except Exception as exc:
            log.debug("clawhub download of %s failed: %s", slug, type(exc).__name__)
            return None
        return bytes(data)

    @staticmethod
    def unzip(data: bytes) -> dict[str, bytes]:
        """``SKILL.md`` and support-folder files of a skill zip; unsafe members are skipped."""
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise HubError("ClawHub sent an archive that is not a zip") from None
        with archive:
            members = [m for m in archive.infolist() if not m.is_dir()][:MAX_ZIP_MEMBERS]
            names = [m.filename.replace("\\", "/") for m in members]
            top = {n.split("/", 1)[0] for n in names}
            strip = ""
            if len(top) == 1 and SKILL_FILE not in names:
                strip = next(iter(top)) + "/"
            total = 0
            files: dict[str, bytes] = {}
            for member, name in zip(members, names, strict=True):
                if stat.S_ISLNK(member.external_attr >> 16):
                    log.debug("clawhub zip: skipped a link (%s)", name)
                    continue
                if strip and not name.startswith(strip):
                    continue
                try:
                    rel = safe_rel_path(name.removeprefix(strip), what="archive member")
                except ValueError:
                    log.debug("clawhub zip: skipped an unsafe member")
                    continue
                if not _kept(rel) or member.file_size > MAX_FILE_BYTES:
                    continue
                total += member.file_size
                if total > MAX_ZIP_BYTES:
                    raise HubError("the ClawHub archive unpacks to more than 25 MiB")
                files[rel] = archive.read(member)
        return files

    @staticmethod
    def inline_files(version: object) -> dict[str, bytes]:
        """Files carried inline by a version record (``files`` as a map or a list)."""
        if not isinstance(version, Mapping):
            return {}
        record = cast(Mapping[str, Any], version)
        listed = record.get("files")
        if listed is None and isinstance(record.get("version"), Mapping):
            listed = cast(Mapping[str, Any], record["version"]).get("files")
        pairs: list[tuple[object, object]] = []
        if isinstance(listed, Mapping):
            pairs = list(cast(Mapping[object, object], listed).items())
        elif isinstance(listed, list):
            for item in cast(list[object], listed):
                if isinstance(item, Mapping):
                    entry = cast(Mapping[str, object], item)
                    pairs.append((entry.get("path") or entry.get("name"), entry.get("content")))
        files: dict[str, bytes] = {}
        for raw_name, content in pairs:
            if not isinstance(raw_name, str) or not isinstance(content, str):
                continue
            try:
                rel = safe_rel_path(raw_name, what="file path")
            except ValueError:
                continue
            data = content.encode("utf-8")
            if _kept(rel) and len(data) <= MAX_FILE_BYTES:
                files[rel] = data
        return files

    async def fetch(self, identifier: str) -> Bundle | None:
        found = await self._detail(identifier)
        if found is None:
            return None
        slug, owner, data = found
        version = await self._version(slug, owner, data)
        if version is None:
            log.info("clawhub: no version found for %s", slug)
            return None
        files: dict[str, bytes] = {}
        archive = await self._download(slug, version, owner)
        if archive:
            files = self.unzip(archive)
        if SKILL_FILE not in files:
            params = {"owner": owner} if owner else None
            record = await self.http.get_json(
                f"{self.base}/skills/{quote(slug)}/versions/{quote(version)}", params=params
            )
            actual = self._owner_of(record) if isinstance(record, Mapping) else None
            if owner and actual and actual.lower() != owner.lower():
                log.info("clawhub version record of %s names another owner; ignored", slug)
                record = None
            files = self.inline_files(record) or files
        if SKILL_FILE not in files:
            return None
        return Bundle(
            name=slug,
            files=files,
            source=self.id,
            identifier=self._ident(slug, owner),
            trust="community",
            metadata={"version": version},
        )


# -- LobeHub --------------------------------------------------------------------------------------


def lobehub_skill_md(agent: Mapping[str, Any], name: str) -> str:
    """A LobeHub agent as a ``SKILL.md`` of our layout. The system role stays untrusted text: the
    install pipeline scans it like any skill."""
    meta_raw = agent.get("meta")
    meta = cast(Mapping[str, Any], meta_raw) if isinstance(meta_raw, Mapping) else agent
    title = _text(meta.get("title"), 200) or name
    description = _text(meta.get("description"), 500) or f"The {title} agent from LobeHub."
    config = agent.get("config")
    role = (
        cast(Mapping[str, Any], config).get("systemRole") if isinstance(config, Mapping) else None
    )
    instructions = (
        role.strip()
        if isinstance(role, str) and role.strip()
        else ("(this agent defines no instructions)")
    )
    front = {
        "name": name,
        "description": description,
        "metadata": {
            "indus": {"tags": list(_tags(meta.get("tags")))},
            "lobehub": {"identifier": str(agent.get("identifier") or name)},
        },
    }
    head = yamlio.dump_plain(front)
    body = f"# {title}\n\n{description}\n\n## Instructions\n\n{instructions}\n"
    return f"---\n{head}---\n\n{body}"


class LobeHubSource:
    """LobeHub agent entries, converted to skills (see the module notes)."""

    id = "lobehub"
    prefix = "lobehub/"

    def __init__(self, http: HubHttp, base: str = LOBEHUB_BASE) -> None:
        self.http = http
        self.base = base.rstrip("/")
        self._index: list[dict[str, Any]] | None = None

    def handles(self, identifier: str) -> bool:
        return identifier.strip().startswith(self.prefix)

    async def agents(self) -> list[dict[str, Any]]:
        if self._index is None:
            data = await self.http.get_json(f"{self.base}/index.json")
            listed = (
                cast(Mapping[str, Any], data).get("agents") if isinstance(data, Mapping) else data
            )
            self._index = [
                dict(cast(Mapping[str, Any], a))
                for a in (cast(list[object], listed) if isinstance(listed, list) else [])
                if isinstance(a, Mapping)
            ]
        return self._index

    @staticmethod
    def _fields(agent: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
        meta_raw = agent.get("meta")
        meta = cast(Mapping[str, Any], meta_raw) if isinstance(meta_raw, Mapping) else agent
        return (
            _text(meta.get("title"), 200),
            _text(meta.get("description"), 200),
            _tags(meta.get("tags")),
        )

    def _meta(self, agent: Mapping[str, Any]) -> SkillMeta | None:
        ident = agent.get("identifier")
        if not isinstance(ident, str) or not _SLUG.match(ident):
            return None
        title, description, tags = self._fields(agent)
        return SkillMeta(
            name=ident,
            description=description or title,
            source=self.id,
            identifier=f"{self.prefix}{ident}",
            trust="community",
            tags=tags,
            extra={"title": title},
        )

    async def search(self, query: str, limit: int) -> list[SkillMeta]:
        rows: list[SkillMeta] = []
        for agent in await self.agents():
            title, description, tags = self._fields(agent)
            if not _matches(query, agent.get("identifier"), title, description, tags):
                continue
            meta = self._meta(agent)
            if meta is not None:
                rows.append(meta)
            if len(rows) >= limit:
                break
        return rows

    def _agent_id(self, identifier: str) -> str:
        ident = identifier.strip().removeprefix(self.prefix).strip("/")
        if not _SLUG.match(ident):
            raise HubError(f"{identifier!r} is not lobehub/<agent id>")
        return ident

    async def resolve(self, identifier: str) -> SkillMeta | None:
        ident = self._agent_id(identifier)
        agent = next((a for a in await self.agents() if a.get("identifier") == ident), None)
        return self._meta(agent) if agent is not None else None

    async def fetch(self, identifier: str) -> Bundle | None:
        ident = self._agent_id(identifier)
        data = await self.http.get_json(f"{self.base}/{quote(ident)}.json")
        if not isinstance(data, Mapping):
            return None
        try:
            name = safe_skill_name(ident.lower())
        except ValueError:
            raise HubError(f"the LobeHub agent id {ident!r} is not a usable skill name") from None
        text = lobehub_skill_md(cast(Mapping[str, Any], data), name)
        return Bundle(
            name=name,
            files={SKILL_FILE: text.encode("utf-8")},
            source=self.id,
            identifier=f"{self.prefix}{ident}",
            trust="community",
            metadata={"converted_from": "lobehub-agent"},
        )


# -- browse.sh ------------------------------------------------------------------------------------


class BrowseShSource:
    """Site-automation skills from browse.sh (see the module notes)."""

    id = "browse-sh"
    prefix = "browse-sh/"

    def __init__(self, http: HubHttp, base: str = BROWSE_SH_BASE) -> None:
        self.http = http
        self.base = base.rstrip("/")
        self._catalogue: list[dict[str, Any]] | None = None

    def handles(self, identifier: str) -> bool:
        return identifier.strip().startswith(self.prefix)

    def slug_of(self, identifier: str) -> str:
        slug = identifier.strip().removeprefix(self.prefix).strip("/")
        host, _, task = slug.partition("/")
        if not _HOST.match(host) or not task or "/" in task or not _SLUG.match(task):
            raise HubError(f"{identifier!r} is not browse-sh/<host>/<task>")
        return slug

    async def catalogue(self) -> list[dict[str, Any]]:
        if self._catalogue is None:
            data = await self.http.get_json(f"{self.base}/api/skills")
            listed = (
                cast(Mapping[str, Any], data).get("skills") if isinstance(data, Mapping) else None
            )
            self._catalogue = [
                dict(cast(Mapping[str, Any], s))
                for s in (cast(list[object], listed) if isinstance(listed, list) else [])
                if isinstance(s, Mapping)
            ]
        return self._catalogue

    def _meta(self, item: Mapping[str, Any]) -> SkillMeta | None:
        slug = item.get("slug")
        name = item.get("name")
        if not isinstance(slug, str) or not isinstance(name, str) or not name.strip():
            return None
        try:
            self.slug_of(self.prefix + slug)
        except HubError:
            return None
        return SkillMeta(
            name=name.strip(),
            description=_text(item.get("description") or item.get("title") or name),
            source=self.id,
            identifier=f"{self.prefix}{slug}",
            trust="community",
            category=str(item.get("category") or ""),
            tags=_tags(item.get("tags")),
            extra={"hostname": str(item.get("hostname") or slug.split("/", 1)[0])},
        )

    async def search(self, query: str, limit: int) -> list[SkillMeta]:
        rows: list[SkillMeta] = []
        for item in await self.catalogue():
            fields = (
                item.get(k)
                for k in ("name", "title", "description", "hostname", "category", "tags")
            )
            if not _matches(query, *fields):
                continue
            meta = self._meta(item)
            if meta is not None:
                rows.append(meta)
            if len(rows) >= limit:
                break
        return rows

    async def _item(self, identifier: str) -> dict[str, Any] | None:
        slug = self.slug_of(identifier)
        return next((i for i in await self.catalogue() if i.get("slug") == slug), None)

    async def resolve(self, identifier: str) -> SkillMeta | None:
        item = await self._item(identifier)
        return self._meta(item) if item is not None else None

    async def fetch(self, identifier: str) -> Bundle | None:
        item = await self._item(identifier)
        meta = self._meta(item) if item is not None else None
        if item is None or meta is None:
            return None
        slug = str(item["slug"])
        detail = await self.http.get_json(f"{self.base}/api/skills/{quote(slug, safe='/.')}")
        url = (
            cast(Mapping[str, Any], detail).get("skillMdUrl")
            if isinstance(detail, Mapping)
            else None
        )
        if not isinstance(url, str) or urlsplit(url).scheme not in ("https", "http"):
            log.info("browse.sh gave no SKILL.md address for %s", slug)
            return None
        data = await self.http.get_bytes(url)
        if data is None:
            return None
        task = slug.split("/", 1)[1]
        name = meta.name if _NAME.match(meta.name) else task.lower()
        try:
            name = safe_skill_name(name)
        except ValueError:
            raise HubError(f"browse.sh skill {slug!r} has no usable name") from None
        return Bundle(
            name=name,
            files={SKILL_FILE: data},
            source=self.id,
            identifier=f"{self.prefix}{slug}",
            trust="community",
            metadata={"hostname": meta.extra.get("hostname", ""), "skill_md_url": url},
        )
