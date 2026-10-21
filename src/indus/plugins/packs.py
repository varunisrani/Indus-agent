# pyright: strict
"""Plugin packs: a shareable list of pinned plugins installed together (P24-44).

A pack is YAML::

    name: research-kit            # metadata may also sit under a `pack:` mapping
    description: …                # optional, as are author and version
    plugins:
      - repo: someone/indus-plugin-x      # owner/repo or a git URL
        ref: <40-hex commit SHA>
        subdir: plugins/x                 # optional
      - name: catalogue-name              # a bare name needs a plugin catalogue
        ref: <40-hex commit SHA>
    config:                       # non-secret seeds for plugins.entries.<id>
      x: {greeting: hello}
    skills: [some-skill]          # shown on the review screen, never installed

Rules: every ``ref`` is a full 40-hex commit SHA (short SHAs, tags and branches fail the whole
pack, naming the entry). Seed keys that look secret (token, secret, password, api key, private
key, credential, auth), the consent keys (``granted_capabilities``, ``capabilities_consent``) and
``allow_*`` trust gates are stripped at any depth, each with a warning: a pack never carries a
secret and never grants anything. A seed never replaces a value the user already has.

Installing is the surface's job (``indus plugins pack install``): one review screen, one
question, then per plugin the normal P13 install, its env questions, enable and its own capability
consent. ``install_pack`` runs that fan-out and keeps going past a failed plugin. ``export_pack``
writes the installed plugins that have git provenance; a local install is left out with a warning.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from indus.foundation import yamlio
from indus.foundation.scope import Scope
from indus.plugins.context import PluginError
from indus.plugins.install import Installer, InstallResult

__all__ = [
    "MAX_PACK_BYTES",
    "Pack",
    "PackError",
    "PackPlugin",
    "PackResult",
    "Resolved",
    "apply_seed",
    "clean_seed",
    "export_pack",
    "install_pack",
    "load_pack",
    "parse_pack",
    "resolve",
    "review_lines",
]

log = logging.getLogger(__name__)

MAX_PACK_BYTES = 1024 * 1024
FETCH_TIMEOUT_S = 15.0
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SECRETISH = re.compile(
    r"token|secret|passw(?:or)?d|api[_-]?key|private[_-]?key|credential|auth", re.IGNORECASE
)
CONSENT_KEYS = frozenset({"granted_capabilities", "capabilities_consent"})
_GITHUB_URL = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s#]+?)(?:\.git)?$")


class PackError(PluginError):
    default_code = "plugins.pack"


@dataclass(frozen=True)
class PackPlugin:
    """One pinned entry: a ``repo`` (owner/repo or git URL) or a bare catalogue ``name``."""

    ref: str
    name: str = ""
    repo: str = ""
    subdir: str = ""

    @property
    def display(self) -> str:
        base = self.repo or self.name or "?"
        return f"{base}/{self.subdir}" if self.repo and self.subdir else base

    @property
    def identifier(self) -> str | None:
        """What ``indus plugins install`` takes; None for a bare name."""
        if not self.repo:
            return None
        if not self.subdir:
            return self.repo
        if "://" in self.repo or self.repo.startswith("git@"):
            return f"{self.repo}#{self.subdir}"
        return f"{self.repo}/{self.subdir}"


@dataclass(frozen=True)
class Pack:
    name: str
    plugins: tuple[PackPlugin, ...]
    description: str = ""
    author: str = ""
    version: str = ""
    config: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: {})
    skills: tuple[str, ...] = ()


# -- seeds ----------------------------------------------------------------------------------------


def _forbidden(key: str) -> str | None:
    if key in CONSENT_KEYS:
        return "consent is asked per plugin, never carried by a pack"
    if key.startswith("allow_"):
        return "trust gates are never carried by a pack"
    if _SECRETISH.search(key):
        return "secrets never travel in a pack (the plugin asks for them at install)"
    return None


def clean_seed(value: object, where: str, warnings: list[str]) -> Any:
    """``value`` with forbidden keys stripped at every depth (one warning each) and anything that
    is not plain data dropped."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, child in cast(Mapping[object, object], value).items():
            if not isinstance(key, str) or not key.strip():
                warnings.append(f"{where}: dropped a key that is not text")
                continue
            why = _forbidden(key)
            if why is not None:
                warnings.append(f"{where}.{key} stripped: {why}")
                continue
            out[key] = clean_seed(child, f"{where}.{key}", warnings)
        return out
    if isinstance(value, list):
        return [clean_seed(child, where, warnings) for child in cast(list[object], value)]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    warnings.append(f"{where}: dropped a value of type {type(value).__name__}")
    return None


def apply_seed(
    tree: MutableMapping[str, Any], plugin_id: str, seed: Mapping[str, Any]
) -> list[str]:
    """Write ``seed`` into ``plugins.entries.<plugin_id>`` of a raw config tree; keys the user
    already set are kept. The lines to show."""
    lines: list[str] = []
    warnings: list[str] = []
    clean = cast(dict[str, Any], clean_seed(seed, f"config.{plugin_id}", warnings))
    lines.extend(f"warning: {w}" for w in warnings)
    plugins = tree.setdefault("plugins", {})
    if not isinstance(plugins, MutableMapping):
        return [*lines, "warning: the plugins section is not a mapping; seed skipped"]
    entries = cast(MutableMapping[str, Any], plugins).setdefault("entries", {})
    if not isinstance(entries, MutableMapping):
        return [*lines, "warning: plugins.entries is not a mapping; seed skipped"]
    entry = cast(MutableMapping[str, Any], entries).setdefault(plugin_id, {})
    if not isinstance(entry, MutableMapping):
        return [*lines, f"warning: plugins.entries.{plugin_id} is not a mapping; seed skipped"]
    target = cast(MutableMapping[str, Any], entry)
    for key, value in clean.items():
        if key in target:
            lines.append(f"plugins.entries.{plugin_id}.{key} already set: your value is kept")
            continue
        target[key] = value
        lines.append(f"plugins.entries.{plugin_id}.{key} seeded from the pack")
    return lines


# -- parsing --------------------------------------------------------------------------------------


def _label(item: object, index: int) -> str:
    if isinstance(item, Mapping):
        entry = cast(Mapping[str, object], item)
        for key in ("name", "repo"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return f"{value.strip()!r}"
    return f"#{index + 1}"


def _entry(item: object, label: str) -> PackPlugin:
    if not isinstance(item, Mapping):
        raise PackError(f"pack plugin entry {label} must be a mapping")
    raw = cast(Mapping[str, object], item)
    name = raw.get("name")
    repo = raw.get("repo") or raw.get("source")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise PackError(f"pack plugin entry {label} has an empty or odd name")
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        raise PackError(f"pack plugin entry {label} has an empty or odd repo")
    name_text = name.strip() if isinstance(name, str) else ""
    repo_text = repo.strip().removeprefix("github:") if isinstance(repo, str) else ""
    if not name_text and not repo_text:
        raise PackError(f"pack plugin entry {label} needs a repo (owner/repo or git URL) or a name")
    ref = raw.get("ref")
    if not isinstance(ref, str) or not _SHA.match(ref.strip()):
        raise PackError(
            f"pack plugin entry {label} has ref {ref!r}: a ref must be a full 40-character "
            "commit SHA (tags, branch names and short SHAs can move)"
        )
    subdir = raw.get("subdir")
    sub_text = ""
    if subdir is not None:
        if not isinstance(subdir, str) or not subdir.strip("/ "):
            raise PackError(f"pack plugin entry {label} has an empty or odd subdir")
        parts = [p for p in subdir.replace("\\", "/").split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            raise PackError(f"pack plugin entry {label}: subdir leaves the repository")
        sub_text = "/".join(parts)
    return PackPlugin(ref.strip().lower(), name_text, repo_text, sub_text)


def parse_pack(text: str, *, source: str = "<pack>") -> tuple[Pack, list[str]]:
    """The pack and its warnings; raises ``PackError`` for anything that makes it unusable."""
    try:
        raw = yamlio.load_safe(text)
    except Exception as exc:
        raise PackError(f"pack {source} is not valid YAML ({type(exc).__name__})") from None
    if not isinstance(raw, Mapping):
        raise PackError(f"pack {source} must be a YAML mapping")
    doc = cast(Mapping[str, object], raw)
    nested = doc.get("pack")
    meta = cast(Mapping[str, object], nested) if isinstance(nested, Mapping) else doc
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PackError(f"pack {source} has no name")
    items = doc.get("plugins")
    if not isinstance(items, list) or not items:
        raise PackError(f"pack {source} lists no plugins")
    entries = tuple(
        _entry(item, _label(item, i)) for i, item in enumerate(cast(list[object], items))
    )
    warnings: list[str] = []
    seeds_raw: object = doc.get("config") or {}
    if not isinstance(seeds_raw, Mapping):
        raise PackError(f"pack {source}: config must map plugin ids to settings")
    seeds: dict[str, Mapping[str, Any]] = {}
    for plugin_id, seed in cast(Mapping[object, object], seeds_raw).items():
        if not isinstance(seed, Mapping):
            raise PackError(f"pack {source}: config for {plugin_id!r} must be a mapping")
        seeds[str(plugin_id)] = cast(
            dict[str, Any],
            clean_seed(cast(Mapping[str, object], seed), f"config.{plugin_id}", warnings),
        )
    skills_raw: object = doc.get("skills") or []
    if not isinstance(skills_raw, list):
        raise PackError(f"pack {source}: skills must be a list")
    skills = tuple(str(s).strip() for s in cast(list[object], skills_raw) if str(s).strip())
    pack = Pack(
        name=name.strip(),
        plugins=entries,
        description=str(meta.get("description") or "").strip(),
        author=str(meta.get("author") or "").strip(),
        version=str(meta.get("version") or "").strip(),
        config=seeds,
        skills=skills,
    )
    return pack, warnings


def _fetch_https(url: str, scope: Scope | None) -> bytes:
    import httpx

    from indus.safety.safe_transport import safe_client

    async def go() -> bytes:
        client = safe_client(scope, timeout=httpx.Timeout(FETCH_TIMEOUT_S))
        async with client, client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise PackError(f"could not fetch the pack: HTTP {resp.status_code}")
            data = b""
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > MAX_PACK_BYTES:
                    raise PackError("the pack is larger than 1 MiB")
            return data

    import asyncio

    try:
        return asyncio.run(go())
    except httpx.HTTPError as exc:
        raise PackError(f"could not fetch the pack ({type(exc).__name__})") from None


def load_pack(
    where: str,
    *,
    scope: Scope | None = None,
    cwd: Path | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> tuple[Pack, list[str]]:
    """A pack from a local file or an ``https://`` URL (≤ 1 MiB)."""
    text = where.strip()
    lowered = text.lower()
    if lowered.startswith("https://"):
        data = fetch(text) if fetch is not None else _fetch_https(text, scope)
        if len(data) > MAX_PACK_BYTES:
            raise PackError("the pack is larger than 1 MiB")
        return parse_pack(data.decode("utf-8-sig", errors="replace"), source=text)
    if "://" in lowered:
        raise PackError("a pack URL must use https:// (or give a local file)")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    if not path.is_file():
        raise PackError(f"no pack file at {path}")
    if path.stat().st_size > MAX_PACK_BYTES:
        raise PackError("the pack is larger than 1 MiB")
    return parse_pack(path.read_text(encoding="utf-8-sig"), source=str(path))


# -- resolution and review ------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    entry: PackPlugin
    identifier: str | None
    problem: str = ""


def resolve(pack: Pack) -> list[Resolved]:
    """Each entry's install identifier; a bare name cannot be resolved (Indus ships no plugin
    catalogue), which the review shows and the install reports as a failure."""
    out: list[Resolved] = []
    for entry in pack.plugins:
        ident = entry.identifier
        if ident is None:
            out.append(Resolved(entry, None, "a bare name needs a plugin catalogue; use repo:"))
        else:
            out.append(Resolved(entry, ident))
    return out


def review_lines(
    pack: Pack, resolved: Sequence[Resolved], warnings: Sequence[str] = ()
) -> list[str]:
    """The review screen shown before any install."""
    title = pack.name + (f" {pack.version}" if pack.version else "")
    lines = [f"Pack: {title}" + (f" by {pack.author}" if pack.author else "")]
    if pack.description:
        lines.append(f"  {pack.description}")
    lines.append(f"Plugins ({len(resolved)}):")
    for row in resolved:
        where = row.identifier or f"cannot install: {row.problem}"
        lines.append(f"  {row.entry.display:<32} {row.entry.ref[:12]}  {where}")
    if pack.config:
        lines.append("Settings it seeds (yours win where set):")
        for plugin_id, seed in pack.config.items():
            for key, value in seed.items():
                lines.append(f"  plugins.entries.{plugin_id}.{key} = {value!r}")
    if pack.skills:
        lines.append(
            "Skills it mentions (not installed; `indus skills install <id>`): "
            + ", ".join(pack.skills)
        )
    lines.extend(f"warning: {w}" for w in warnings)
    lines.append(
        f"Installing runs third-party code from {len(resolved)} source(s). Each plugin's "
        "capabilities are asked for separately; a pack grants nothing."
    )
    return lines


# -- installing -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PackResult:
    display: str
    ok: bool
    name: str = ""
    error: str = ""


type Finish = Callable[[InstallResult, Mapping[str, Any] | None], str | None]
"""After a plugin installed: env questions, enable, capability consent, seeds. Returns an error
text when that plugin should count as failed."""


def install_pack(
    pack: Pack,
    resolved: Sequence[Resolved],
    installer: Installer,
    *,
    finish: Finish,
    force: bool = False,
    confirm_scan: Callable[[Any], bool] | None = None,
    say: Callable[[str], None] = lambda _line: None,
) -> list[PackResult]:
    """Install every resolved entry at its pin; one failure never stops the others."""
    results: list[PackResult] = []
    for row in resolved:
        display = row.entry.display
        if row.identifier is None:
            results.append(PackResult(display, False, error=row.problem))
            say(f"✗ {display}: {row.problem}")
            continue
        say(f"Installing {display} at {row.entry.ref[:12]}…")
        try:
            done = installer.install(
                row.identifier, ref=row.entry.ref, force=force, confirm=confirm_scan
            )
        except PluginError as exc:
            results.append(PackResult(display, False, error=str(exc)))
            say(f"✗ {display}: {exc}")
            continue
        except Exception as exc:  # one broken plugin must not end the fan-out
            log.warning("pack install of %s failed", display, exc_info=True)
            results.append(PackResult(display, False, error=f"{type(exc).__name__}: {exc}"))
            say(f"✗ {display}: {type(exc).__name__}")
            continue
        seed = pack.config.get(done.name)
        if seed is None and row.entry.name:
            seed = pack.config.get(row.entry.name)
        problem = finish(done, seed)
        if problem:
            results.append(PackResult(display, False, done.name, problem))
            say(f"✗ {display}: {problem}")
            continue
        results.append(PackResult(display, True, done.name))
        say(f"✓ {display} installed as {done.name}")
    return results


# -- exporting ------------------------------------------------------------------------------------


def _repo_of(source: str) -> tuple[str, str]:
    base, _, sub = source.partition("#")
    match = _GITHUB_URL.match(base.strip())
    repo = f"{match.group(1)}/{match.group(2)}" if match else base.strip()
    return repo, sub.strip("/")


def export_pack(
    scope: Scope,
    tree: Mapping[str, Any],
    *,
    name: str = "my-indus-pack",
    only: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    """Pack YAML for the installed plugins with git provenance, and the warnings. ``tree`` is the
    raw config (for seeds); ``only`` limits the export to those plugin names."""
    records = Installer(scope).records()
    plugins_dir = scope.home / "plugins"
    names = (
        sorted(p.name for p in plugins_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
        if plugins_dir.is_dir()
        else []
    )
    if only is not None:
        names = [n for n in names if n in set(only)]
    entries: list[dict[str, Any]] = []
    seeds: dict[str, Any] = {}
    warnings: list[str] = []
    plugins_cfg = tree.get("plugins")
    all_entries: Mapping[str, Any] = {}
    if isinstance(plugins_cfg, Mapping):
        found = cast(Mapping[str, Any], plugins_cfg).get("entries")
        if isinstance(found, Mapping):
            all_entries = cast(Mapping[str, Any], found)
    for plugin in names:
        record = records.get(plugin) or {}
        revision = str(record.get("revision") or "")
        source = str(record.get("source") or "")
        if record.get("kind") not in ("github", "git") or not _SHA.match(revision) or not source:
            warnings.append(
                f"{plugin}: no git provenance (installed from a local folder); left out"
            )
            continue
        repo, sub = _repo_of(source)
        row: dict[str, Any] = {"repo": repo, "ref": revision.lower()}
        if sub:
            row["subdir"] = sub
        entries.append(row)
        seed = all_entries.get(plugin)
        if isinstance(seed, Mapping):
            clean = cast(
                dict[str, Any], clean_seed(cast(Mapping[str, object], seed), f"config.{plugin}", [])
            )
            if clean:
                seeds[plugin] = clean
    doc: dict[str, Any] = {
        "name": name,
        "description": "Exported by `indus plugins pack export`.",
        "version": "1.0.0",
        "plugins": entries,
    }
    if seeds:
        doc["config"] = seeds
    text = yamlio.dump_plain(doc)
    if warnings:
        text = "".join(f"# not exported: {w}\n" for w in warnings) + text
    return text, warnings
