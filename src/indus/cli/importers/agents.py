"""Mapping rules for ``indus import-agent`` (P24-18): Claude Code and Codex homes → a profile.

``build_plan`` reads only the files the mappings need and returns a ``Plan``: one ``Item`` per
mapping with its status (``import``, ``conflict``, ``skipped``, ``in place``, ``error``), the
source files it depends on and what applying it does. Nothing is written while planning, so the
preview is exact. ``apply_plan`` runs the ``import`` items: file actions one by one, then every
settings change in one round-trip write (comments and order kept).

Never read, digested or copied: credential files (``CREDENTIAL_FILES`` and key-like suffixes).
MCP env values are imported as ``${NAME}`` references only; credential headers are dropped and
reported by name; a server whose URL or arguments look like they hold a credential is refused.

``import-sync.json`` in the profile home records, per agent, the source folders, the digest of
every source file the plan used and the digest of every skill copy written, so ``--sync`` can
re-apply only the mappings whose source files changed and refresh a skill only while the copy is
still the one it wrote.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from indus.core.prompt import texts
from indus.core.prompt.context_files import AGENTS_NAMES, CLAUDE_NAMES, NATIVE_NAMES
from indus.core.prompt.persona import persona_path
from indus.foundation import config
from indus.foundation.config import writer
from indus.foundation.errors import IndusError
from indus.foundation.fsio import atomic_write_json, atomic_write_text
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.learning.memory.store import DELIMITER, MemoryStore, Op
from indus.mcp.config import parse_server

__all__ = [
    "AGENTS",
    "Agent",
    "CHAR_CAP",
    "CREDENTIAL_FILES",
    "DEFAULT_DIRS",
    "LABELS",
    "MANIFEST_FILE",
    "SKILL_FOLDERS",
    "Item",
    "Manifest",
    "Plan",
    "apply_plan",
    "build_plan",
    "extract_entries",
    "is_credential_file",
    "map_permission",
    "translate_server",
]

type Agent = Literal["claude-code", "codex"]
type Status = Literal["import", "conflict", "skipped", "in place", "error"]

AGENTS: tuple[Agent, ...] = ("claude-code", "codex")
LABELS: Mapping[str, str] = {"claude-code": "Claude Code", "codex": "Codex"}
DEFAULT_DIRS: Mapping[str, str] = {"claude-code": ".claude", "codex": ".codex"}
SKILL_FOLDERS: Mapping[str, str] = {"claude-code": "claude-code-imports", "codex": "codex-imports"}
CHAR_CAP = 20_000
MANIFEST_FILE = "import-sync.json"
SKILL_FILE_CAP = 5 * 1024 * 1024
CREDENTIAL_FILES = frozenset(
    {".credentials.json", "credentials.json", "auth.json", ".env", ".netrc", ".npmrc", ".pypirc"}
)
_CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
_CREDENTIAL_HEADER_WORDS = ("authorization", "cookie", "token", "secret", "key", "auth", "password")
_REFERENCE = re.compile(r"\$\{(?:env:)?[A-Za-z_][A-Za-z0-9_]*\}")
_BLOCK_BEGIN = "<!-- indus import-agent: {agent} (begin) -->"
_BLOCK_END = "<!-- indus import-agent: {agent} (end) -->"


def is_credential_file(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in CREDENTIAL_FILES
        or lowered.endswith(_CREDENTIAL_SUFFIXES)
        or lowered.startswith(("id_rsa", "id_ed25519", "id_ecdsa"))
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    """sha256 of the file (of the copyable tree for a folder), ``missing`` when absent,
    ``unreadable`` when it cannot be read. Credential files are never read."""
    if is_credential_file(path.name):
        return "skipped"
    if path.is_dir() and not path.is_symlink():
        return "tree:" + tree_digest(path)
    try:
        return _sha(path.read_bytes())
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def _copyable(folder: Path) -> Iterable[tuple[Path, str]]:
    """Regular, non-credential files under ``folder`` (no symlinks followed) with their
    relative paths, and the reasons others were left behind (as ``(path, reason)``)."""
    for base, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink():
                yield path, "a symlink"
            elif is_credential_file(name):
                yield path, "a credential file"
            elif path.stat().st_size > SKILL_FILE_CAP:
                yield path, "larger than 5 MiB"
            else:
                yield path, ""


def tree_digest(folder: Path) -> str:
    digest = hashlib.sha256()
    for path, why in _copyable(folder):
        if why:
            continue
        rel = path.relative_to(folder).as_posix()
        digest.update(f"{rel}\0{_sha(path.read_bytes())}\0".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


# -- items and plans -------------------------------------------------------------------------------


@dataclass
class Item:
    kind: str
    source: str
    target: str
    status: Status
    reason: str = ""
    sources: tuple[Path, ...] = ()
    notes: list[str] = field(default_factory=list[str])
    run: Callable[[], None] | None = None
    edit: Callable[[dict[str, Any]], None] | None = None
    skill: tuple[str, Path] | None = None
    outcome: str = ""


@dataclass
class Plan:
    agent: Agent
    source: Path
    project: Path | None
    target: Path
    items: list[Item] = field(default_factory=list[Item])
    watched: set[Path] = field(default_factory=set[Path])
    """Source files read even when they gave no item (absent today, say), so that one appearing
    later counts as a change for ``--sync``."""

    def count(self, status: Status) -> int:
        return sum(1 for item in self.items if item.status == status)

    def files(self) -> set[Path]:
        return {path for item in self.items for path in item.sources} | self.watched


# -- manifest --------------------------------------------------------------------------------------


@dataclass
class Manifest:
    agents: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    @classmethod
    def read(cls, home: Path) -> Manifest:
        try:
            data = json.loads((home / MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict) or not isinstance(data.get("agents"), dict):
            return cls()
        agents = cast(dict[str, object], data["agents"])
        return cls({k: cast(dict[str, Any], v) for k, v in agents.items() if isinstance(v, dict)})

    def write(self, home: Path) -> None:
        atomic_write_json(home / MANIFEST_FILE, {"version": 1, "agents": self.agents}, indent=2)

    def skills(self, agent: str) -> dict[str, str]:
        raw = self.agents.get(agent, {}).get("skills")
        return (
            {str(k): str(v) for k, v in cast(dict[str, object], raw).items()}
            if isinstance(raw, dict)
            else {}
        )

    def files(self, agent: str) -> dict[str, str]:
        raw = self.agents.get(agent, {}).get("files")
        return (
            {str(k): str(v) for k, v in cast(dict[str, object], raw).items()}
            if isinstance(raw, dict)
            else {}
        )


# -- reading sources -------------------------------------------------------------------------------


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as exc:
        return None, f"could not read {path.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} is not a JSON object"
    return cast(dict[str, Any], data), ""


def _read_toml(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig")), ""
    except FileNotFoundError:
        return None, ""
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"could not read {path.name}: {exc}"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


# -- permission rules ------------------------------------------------------------------------------

_BASH_RULE = re.compile(r"Bash\((?P<inner>.*)\)", re.DOTALL)


def map_permission(rule: str) -> tuple[list[str], str]:
    """``Bash(...)`` rule → command patterns, or ``([], why not)``.

    ``Bash(git status)`` → ``git status``; ``Bash(npm run test:*)`` (a prefix rule) → the prefix
    itself and ``npm run test *``; a bare ``Bash`` or ``Bash(*)`` is refused as too broad; other
    tools have no Indus equivalent.
    """
    text = rule.strip()
    if text == "Bash":
        return [], "too broad (every command)"
    match = _BASH_RULE.fullmatch(text)
    if match is None:
        tool = text.split("(", 1)[0] or text
        return [], f"{tool} rules have no Indus equivalent"
    inner = " ".join(match.group("inner").split())
    if inner in ("", "*", ":*"):
        return [], "too broad (every command)"
    if inner.endswith(":*"):
        prefix = inner[:-2].rstrip()
        return [prefix, f"{prefix} *"], ""
    return [inner], ""


def _rules(settings: Mapping[str, Any], key: str) -> list[str]:
    permissions = settings.get("permissions")
    if not isinstance(permissions, Mapping):
        return []
    rules = cast(Mapping[str, object], permissions).get(key)
    if not isinstance(rules, list):
        return []
    return [r for r in cast(list[object], rules) if isinstance(r, str)]


# -- MCP servers -----------------------------------------------------------------------------------


def _looks_secret(text: str) -> bool:
    return redact_text(text) != text


def _reference(name: str, value: object) -> str:
    text = str(value) if value is not None else ""
    return text if _REFERENCE.fullmatch(text.strip()) else f"${{{name}}}"


def _header_is_credential(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in _CREDENTIAL_HEADER_WORDS)


def translate_server(
    name: str, raw: Mapping[str, Any], *, project: bool
) -> tuple[dict[str, Any] | None, list[str], str]:
    """One source MCP entry → ``(our entry, notes, refusal)``; ``refusal`` is empty on success.

    Env values become ``${NAME}`` references (the value is never copied); Codex
    ``bearer_token_env_var`` / ``env_http_headers`` and ``env_vars`` become references too.
    """
    notes: list[str] = []
    entry: dict[str, Any] = {}
    command, url = raw.get("command"), raw.get("url")
    if isinstance(command, str) and command:
        args = [str(a) for a in cast(list[object], raw.get("args") or [])]
        if any(_looks_secret(part) for part in (command, *args)):
            return (
                None,
                [],
                "an argument looks like a credential; add the server by hand with ${NAME}",
            )
        entry["command"] = command
        if args:
            entry["args"] = args
        env: dict[str, str] = {}
        source_env = raw.get("env")
        if isinstance(source_env, Mapping):
            for key, value in cast(Mapping[object, object], source_env).items():
                env[str(key)] = _reference(str(key), value)
        passed = raw.get("env_vars")
        if isinstance(passed, list):
            for key in cast(list[object], passed):
                if isinstance(key, str) and key:
                    env.setdefault(key, f"${{{key}}}")
        if env:
            entry["env"] = env
            names = sorted(k for k, v in env.items() if v == f"${{{k}}}")
            if names:
                notes.append(
                    "env imported by name only; set the values with `indus config set NAME "
                    f"VALUE`: {', '.join(names)}"
                )
        cwd = raw.get("cwd")
        if isinstance(cwd, str) and cwd:
            entry["cwd"] = cwd
    elif isinstance(url, str) and url:
        if _looks_secret(url) or "@" in url.split("//", 1)[-1].split("/", 1)[0]:
            return None, [], "its URL looks like it carries a credential; add it by hand"
        entry["url"] = url
        kind = str(raw.get("type") or raw.get("transport") or "").lower()
        if kind == "sse":
            entry["transport"] = "sse"
        elif kind in ("http", "streamable-http", "streamable_http"):
            entry["transport"] = "http"
        headers: dict[str, str] = {}
        dropped: list[str] = []
        for table in ("headers", "http_headers"):
            source_headers = raw.get(table)
            if not isinstance(source_headers, Mapping):
                continue
            for key, value in cast(Mapping[object, object], source_headers).items():
                header, text = str(key), str(value)
                bare = text.removeprefix("Bearer ").strip()
                if _REFERENCE.fullmatch(bare):
                    headers[header] = text
                elif _header_is_credential(header) or _looks_secret(text):
                    dropped.append(header)
                else:
                    headers[header] = text
        env_headers = raw.get("env_http_headers")
        if isinstance(env_headers, Mapping):
            for key, value in cast(Mapping[object, object], env_headers).items():
                headers[str(key)] = f"${{{value}}}"
        bearer = raw.get("bearer_token_env_var")
        if isinstance(bearer, str) and bearer:
            headers["Authorization"] = f"Bearer ${{{bearer}}}"
        if headers:
            entry["headers"] = headers
        if dropped:
            notes.append(
                f"credential headers left out (add them as ${{NAME}}): {', '.join(sorted(dropped))}"
            )
    else:
        return None, [], "it has neither a command nor a url"
    if raw.get("enabled") is False or raw.get("disabled") is True:
        entry["enabled"] = False
    if project:
        entry["enabled"] = False
        notes.append(f"defined by the project: imported disabled (`indus mcp enable {name}`)")
    _server, _warnings, problem = parse_server(name, entry)
    if problem:
        return None, [], f"not a valid server here: {problem}"
    return entry, notes, ""


# -- memories --------------------------------------------------------------------------------------

_HEADING = re.compile(r"(#{1,6})\s+(.*\S)\s*$")
_BULLET = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")


def extract_entries(text: str) -> list[str]:
    """Memory entries of a markdown file: each bullet and paragraph is one entry, prefixed with
    the headings above it; code blocks and table rows are left out; duplicates dropped."""
    entries: list[str] = []
    headings: list[str] = []
    paragraph: list[str] = []
    fenced = False

    def emit(body: str) -> None:
        trail = " / ".join(h for h in headings if h)
        entries.append(f"{trail}: {body}" if trail else body)

    def flush() -> None:
        if paragraph:
            emit(" ".join(paragraph))
            paragraph.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _HEADING.fullmatch(stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            headings[:] = [*headings[: level - 1], *([""] * (level - 1 - len(headings)))]
            headings.append(heading.group(2))
            continue
        bullet = _BULLET.fullmatch(line)
        if bullet:
            flush()
            emit(bullet.group(1))
            continue
        if not stripped or (stripped.startswith("|") and stripped.endswith("|")):
            flush()
            continue
        paragraph.append(stripped)
    flush()
    seen: set[str] = set()
    unique: list[str] = []
    for entry in entries:
        key = " ".join(entry.split()).lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(entry.strip())
    return unique


def _capped(entries: Sequence[str], cap: int) -> tuple[list[str], int]:
    kept: list[str] = []
    size = 0
    for entry in entries:
        extra = len(entry) + (len(DELIMITER) if kept else 0)
        if size + extra > cap:
            continue
        kept.append(entry)
        size += extra
    return kept, len(entries) - len(kept)


# -- the planner -----------------------------------------------------------------------------------


@dataclass
class _Context:
    scope: Scope
    agent: Agent
    source: Path
    project: Path | None
    overwrite: bool
    managed_skills: Mapping[str, str]
    user_home: Path | None
    settings: dict[str, Any] | None
    settings_error: str
    plan: Plan
    servers_seen: dict[str, Path] = field(default_factory=dict[str, Path])

    def show(self, path: Path) -> str:
        if self.user_home is not None:
            try:
                return "~/" + path.relative_to(self.user_home).as_posix()
            except ValueError:
                pass
        return str(path)

    def add(self, item: Item) -> Item:
        self.plan.items.append(item)
        return item


def _persona_item(ctx: _Context, path: Path) -> None:
    target = persona_path(ctx.scope.home)
    label = LABELS[ctx.agent]
    shown = f"{target.name} (persona overlay)"
    if not path.is_file():
        ctx.add(Item("instructions", ctx.show(path), shown, "skipped", "not found", (path,)))
        return
    text = _read_text(path)
    if text is None:
        ctx.add(Item("instructions", ctx.show(path), shown, "error", "could not read it", (path,)))
        return
    body = text.strip()
    notes: list[str] = []
    if len(body) > CHAR_CAP:
        body = body[:CHAR_CAP].rstrip()
        notes.append(f"cut to the first {CHAR_CAP:,} characters")
    begin, end = _BLOCK_BEGIN.format(agent=ctx.agent), _BLOCK_END.format(agent=ctx.agent)
    block = f"{begin}\n## Instructions brought over from {label}\n\n{body}\n{end}"
    current = _read_text(target) if target.is_file() else None
    base = current if current is not None and current.strip() else texts.DEFAULT_IDENTITY
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(base):
        updated = pattern.sub(lambda _m: block, base, count=1)
    else:
        updated = base.rstrip() + "\n\n" + block + "\n"
    if not body:
        ctx.add(
            Item("instructions", ctx.show(path), shown, "skipped", "the file is empty", (path,))
        )
        return
    if current is not None and updated == current:
        ctx.add(
            Item("instructions", ctx.show(path), shown, "skipped", "already up to date", (path,))
        )
        return
    if current is None or not current.strip():
        notes.append("PERSONA.md starts from the built-in identity, then the imported block")

    def write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, updated)

    ctx.add(Item("instructions", ctx.show(path), shown, "import", "", (path,), notes, run=write))


def _project_context_item(ctx: _Context, names: Sequence[str]) -> None:
    project = ctx.project
    assert project is not None
    found = next((project / n for n in names if (project / n).is_file()), None)
    if found is None:
        return
    shadows = [*NATIVE_NAMES]
    if names == CLAUDE_NAMES:
        shadows += [*AGENTS_NAMES]
    shadow = next((project / n for n in shadows if (project / n).is_file()), None)
    if shadow is not None:
        why = f"{shadow.name} in the project is read first, so this file is not used"
        ctx.add(Item("project context", str(found), "project context", "skipped", why, (found,)))
        return
    why = "Indus reads it where it is as project context; nothing to copy"
    ctx.add(Item("project context", str(found), "project context", "in place", why, (found,)))


def _allow_or_deny(
    ctx: _Context, settings: Mapping[str, Any], path: Path, key: str, *, allow_ok: bool = True
) -> None:
    rules = _rules(settings, key)
    if not rules:
        return
    is_allow = key == "allow"
    kind = "allow rules" if is_allow else "deny rules"
    target = "command_allowlist" if is_allow else "approvals.deny"
    source = f"{ctx.show(path)} permissions.{key}"
    if is_allow and not allow_ok:
        why = "allow rules a repository commits are not imported (the allowlist is profile-wide)"
        ctx.add(Item(kind, source, target, "skipped", why, (path,)))
        return
    patterns: list[str] = []
    unmapped: list[str] = []
    for rule in rules:
        mapped, why = map_permission(rule)
        if mapped:
            patterns.extend(mapped)
        else:
            unmapped.append(f"{rule} ({why})")
    notes = [f"not mapped: {', '.join(unmapped)}"] if unmapped else []
    if not patterns:
        ctx.add(Item(kind, source, target, "skipped", "no Bash rules to map", (path,), notes))
        return
    if ctx.settings is None:
        ctx.add(Item(kind, source, target, "error", ctx.settings_error, (path,), notes))
        return
    if is_allow:
        current = ctx.settings.get("command_allowlist")
    else:
        approvals = ctx.settings.get("approvals")
        current = (
            cast(Mapping[str, Any], approvals).get("deny")
            if isinstance(approvals, Mapping)
            else None
        )
    existing = [str(x) for x in cast(list[object], current)] if isinstance(current, list) else []
    new = [p for p in dict.fromkeys(patterns) if p not in existing]
    if not new:
        ctx.add(Item(kind, source, target, "skipped", "already present", (path,), notes))
        return
    if not is_allow:
        notes.append("deny rules stay behind the approval floors and block before any allow rule")

    def edit(raw: dict[str, Any]) -> None:
        if is_allow:
            have = raw.get("command_allowlist")
            listed = [str(x) for x in cast(list[object], have)] if isinstance(have, list) else []
            raw["command_allowlist"] = [*listed, *(p for p in new if p not in listed)]
        else:
            section = raw.get("approvals")
            table = dict(cast(Mapping[str, Any], section)) if isinstance(section, Mapping) else {}
            have = table.get("deny")
            listed = [str(x) for x in cast(list[object], have)] if isinstance(have, list) else []
            table["deny"] = [*listed, *(p for p in new if p not in listed)]
            raw["approvals"] = table

    shown = f"{target}: {', '.join(new)}"
    ctx.add(Item(kind, source, shown, "import", "", (path,), notes, edit=edit))


def _servers_from(
    ctx: _Context, servers: Mapping[str, Any], origin: Path, *, project: bool
) -> None:
    if ctx.settings is None:
        for name in servers:
            target = f"mcp_servers.{name}"
            ctx.add(
                Item("mcp server", ctx.show(origin), target, "error", ctx.settings_error, (origin,))
            )
        return
    existing_raw = ctx.settings.get("mcp_servers")
    existing = cast(Mapping[str, Any], existing_raw) if isinstance(existing_raw, Mapping) else {}
    for name, raw in servers.items():
        target = f"mcp_servers.{name}"
        source = f"{ctx.show(origin)} ({name})"
        first = ctx.servers_seen.setdefault(name, origin)
        if first != origin:
            why = f"also defined in {ctx.show(first)}, which is used"
            ctx.add(Item("mcp server", source, target, "skipped", why, (origin,)))
            continue
        if not isinstance(raw, Mapping):
            ctx.add(Item("mcp server", source, target, "error", "not an object", (origin,)))
            continue
        entry, notes, refusal = translate_server(
            name, cast(Mapping[str, Any], raw), project=project
        )
        if entry is None:
            ctx.add(Item("mcp server", source, target, "error", refusal, (origin,)))
            continue
        if name in existing:
            if dict(cast(Mapping[str, Any], existing[name])) == entry:
                ctx.add(Item("mcp server", source, target, "skipped", "already present", (origin,)))
                continue
            if not ctx.overwrite:
                why = "a server with this name exists (--overwrite replaces it)"
                ctx.add(Item("mcp server", source, target, "conflict", why, (origin,), notes))
                continue

        def edit(
            raw_settings: dict[str, Any], name: str = name, entry: dict[str, Any] = entry
        ) -> None:
            table_raw = raw_settings.get("mcp_servers")
            table = (
                dict(cast(Mapping[str, Any], table_raw)) if isinstance(table_raw, Mapping) else {}
            )
            table[name] = entry
            raw_settings["mcp_servers"] = table

        ctx.add(Item("mcp server", source, target, "import", "", (origin,), notes, edit=edit))


def _skill_items(ctx: _Context, folder: Path) -> None:
    if not folder.is_dir():
        return
    root = ctx.scope.home / "skills" / SKILL_FOLDERS[ctx.agent]
    for skill in sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()):
        if not (skill / "SKILL.md").is_file():
            continue
        target = root / skill.name
        shown = f"skills/{SKILL_FOLDERS[ctx.agent]}/{skill.name}"
        files = [p for p, why in _copyable(skill) if not why]
        left = [f"{p.relative_to(skill).as_posix()} ({why})" for p, why in _copyable(skill) if why]
        notes = [f"left behind: {', '.join(left)}"] if left else []
        sources = (skill,)
        if target.exists():
            ours = ctx.managed_skills.get(skill.name)
            current = tree_digest(target)
            if current == tree_digest(skill):
                ctx.add(
                    Item("skill", ctx.show(skill), shown, "skipped", "already up to date", sources)
                )
                continue
            if not ctx.overwrite and ours != current:
                why = (
                    "changed here since it was imported (--overwrite replaces it)"
                    if ours
                    else "a skill with this name exists (--overwrite replaces it)"
                )
                ctx.add(Item("skill", ctx.show(skill), shown, "conflict", why, sources, notes))
                continue

        def copy(skill: Path = skill, target: Path = target, files: list[Path] = files) -> None:
            staging = target.with_name(f".{target.name}.importing")
            if staging.exists():
                shutil.rmtree(staging)
            for path in files:
                dest = staging / path.relative_to(skill)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)

        item = Item("skill", ctx.show(skill), shown, "import", "", sources, notes, run=copy)
        item.skill = (skill.name, target)
        ctx.add(item)


def _memory_item(ctx: _Context, folder: Path) -> None:
    files = sorted(folder.glob("*.md")) if folder.is_dir() else []
    if not files:
        return
    shown = "memories/MEMORY.md (saved notes)"
    sources = tuple(files)
    incoming: list[str] = []
    for path in files:
        text = _read_text(path)
        if text is not None:
            incoming.extend(extract_entries(text))
    capped, over_cap = _capped(incoming, CHAR_CAP)
    store = MemoryStore.for_scope(ctx.scope)
    notes: list[str] = []
    if over_cap:
        notes.append(f"{over_cap} entries past the {CHAR_CAP:,}-character import cap left out")
    if not store.enabled("memory"):
        ctx.add(
            Item("memories", ctx.show(folder), shown, "skipped", "saved notes are off", sources)
        )
        return
    current = store.entries("memory")
    seen = {" ".join(e.split()).lower() for e in current}
    limit = store.limit("memory")
    size = len(DELIMITER.join(current))
    fitting: list[str] = []
    no_room = 0
    for entry in capped:
        key = " ".join(entry.split()).lower()
        if key in seen:
            continue
        extra = len(entry) + (len(DELIMITER) if (current or fitting) else 0)
        if size + extra > limit:
            no_room += 1
            continue
        fitting.append(entry)
        seen.add(key)
        size += extra
    if no_room:
        notes.append(
            f"{no_room} entries do not fit memory.notes_char_limit ({limit:,} chars); raise it "
            "and run the import again to bring them over"
        )
    if not fitting:
        why = "nothing new fits" if no_room else "nothing new to add"
        ctx.add(Item("memories", ctx.show(folder), shown, "skipped", why, sources, notes))
        return

    def add() -> None:
        result = store.apply("memory", [Op("add", content=entry) for entry in fitting])
        if not result.ok:
            raise OSError(result.message or result.kind)

    target = f"{shown}: {len(fitting)} entries"
    ctx.add(Item("memories", ctx.show(folder), target, "import", "", sources, notes, run=add))


def _skipped_folder(ctx: _Context, folder: Path, kind: str, why: str) -> None:
    if folder.is_dir() and any(folder.glob("*.md")):
        ctx.add(Item(kind, ctx.show(folder), "-", "skipped", why))


def _claude_code(ctx: _Context) -> None:
    home = ctx.source
    _persona_item(ctx, home / "CLAUDE.md")
    settings_path = home / "settings.json"
    settings, problem = _read_json(settings_path)
    if problem:
        ctx.add(Item("settings", ctx.show(settings_path), "-", "error", problem, (settings_path,)))
    settings = settings or {}
    _allow_or_deny(ctx, settings, settings_path, "allow")
    _allow_or_deny(ctx, settings, settings_path, "deny")
    sibling = home.parent / ".claude.json"
    ctx.plan.watched |= {settings_path, sibling}
    servers_doc, problem = _read_json(sibling)
    if problem:
        ctx.add(Item("mcp servers", ctx.show(sibling), "-", "error", problem, (sibling,)))
    for doc, origin in ((servers_doc or {}, sibling), (settings, settings_path)):
        servers = doc.get("mcpServers")
        if isinstance(servers, Mapping) and servers:
            _servers_from(ctx, cast(Mapping[str, Any], servers), origin, project=False)
    _skill_items(ctx, home / "skills")
    _skipped_folder(
        ctx, home / "commands", "slash commands", "no direct equivalent; turn them into skills"
    )
    if ctx.project is not None:
        project = ctx.project
        _project_context_item(ctx, CLAUDE_NAMES)
        shared = project / ".claude" / "settings.json"
        local = project / ".claude" / "settings.local.json"
        for path, allow_ok in ((shared, False), (local, True)):
            doc, problem = _read_json(path)
            if problem:
                ctx.add(Item("settings", str(path), "-", "error", problem, (path,)))
            if doc:
                _allow_or_deny(ctx, doc, path, "allow", allow_ok=allow_ok)
                _allow_or_deny(ctx, doc, path, "deny")
        mcp_path = project / ".mcp.json"
        ctx.plan.watched |= {shared, local, mcp_path}
        doc, problem = _read_json(mcp_path)
        if problem:
            ctx.add(Item("mcp servers", str(mcp_path), "-", "error", problem, (mcp_path,)))
        servers = (doc or {}).get("mcpServers")
        if isinstance(servers, Mapping) and servers:
            _servers_from(ctx, cast(Mapping[str, Any], servers), mcp_path, project=True)


def _codex(ctx: _Context) -> None:
    home = ctx.source
    _persona_item(ctx, home / "AGENTS.md")
    config_path = home / "config.toml"
    ctx.plan.watched.add(config_path)
    doc, problem = _read_toml(config_path)
    if problem:
        ctx.add(Item("settings", ctx.show(config_path), "-", "error", problem, (config_path,)))
    servers = (doc or {}).get("mcp_servers")
    if isinstance(servers, Mapping) and servers:
        _servers_from(ctx, cast(Mapping[str, Any], servers), config_path, project=False)
    _memory_item(ctx, home / "memories")
    _skill_items(ctx, home / "skills")
    _skipped_folder(
        ctx, home / "prompts", "custom prompts", "no direct equivalent; turn them into skills"
    )
    if ctx.project is not None:
        _project_context_item(ctx, AGENTS_NAMES)
        project_config = ctx.project / ".codex" / "config.toml"
        ctx.plan.watched.add(project_config)
        doc, problem = _read_toml(project_config)
        if problem:
            item = Item("settings", str(project_config), "-", "error", problem, (project_config,))
            ctx.add(item)
        servers = (doc or {}).get("mcp_servers")
        if isinstance(servers, Mapping) and servers:
            _servers_from(ctx, cast(Mapping[str, Any], servers), project_config, project=True)


def build_plan(
    scope: Scope,
    agent: Agent,
    source: Path,
    *,
    project: Path | None = None,
    overwrite: bool = False,
    manifest: Manifest | None = None,
    user_home: Path | None = None,
) -> Plan:
    """Read the sources and decide every mapping; nothing is written."""
    view = config.load(scope)
    settings: dict[str, Any] | None = None
    settings_error = ""
    if view.error is not None:
        settings_error = f"the settings file cannot be read right now ({view.error}); fix it first"
    else:
        settings = view.user_raw()
    ctx = _Context(
        scope=scope,
        agent=agent,
        source=source,
        project=project,
        overwrite=overwrite,
        managed_skills=(manifest or Manifest()).skills(agent),
        user_home=user_home,
        settings=settings,
        settings_error=settings_error,
        plan=Plan(agent, source, project, scope.home),
    )
    if agent == "claude-code":
        _claude_code(ctx)
    else:
        _codex(ctx)
    return ctx.plan


# -- applying --------------------------------------------------------------------------------------


def apply_plan(scope: Scope, items: Sequence[Item]) -> None:
    """Run every ``import`` item; each ends with ``outcome`` ``done`` or an error text (and
    status ``error``). Settings changes are written together through the round-trip writer."""
    edits: list[Item] = []
    for item in items:
        if item.status != "import":
            continue
        if item.edit is not None:
            edits.append(item)
            continue
        if item.run is None:
            continue
        try:
            item.run()
            item.outcome = "done"
        except (OSError, ValueError) as exc:
            item.status = "error"
            item.outcome = f"{type(exc).__name__}: {exc}"
    if not edits:
        return
    view = config.load(scope)
    if view.error is not None:
        for item in edits:
            item.status, item.outcome = "error", f"the settings file cannot be read ({view.error})"
        return
    raw = view.user_raw()
    for item in edits:
        assert item.edit is not None
        item.edit(raw)
    try:
        writer.save(scope, raw, replace=True)
    except IndusError as exc:
        for item in edits:
            item.status, item.outcome = "error", exc.message
        return
    except OSError as exc:
        for item in edits:
            item.status, item.outcome = "error", f"could not write the settings: {exc}"
        return
    for item in edits:
        item.outcome = "done"


def record(
    home: Path,
    plan: Plan,
    *,
    overwrite: bool,
    stamp: str,
    manifest: Manifest | None = None,
) -> Manifest:
    """Register ``plan``'s sources in ``import-sync.json`` after a real run. A source whose item
    failed keeps its previous digest so the next ``--sync`` retries it."""
    manifest = manifest or Manifest.read(home)
    entry = dict(manifest.agents.get(plan.agent, {}))
    files = manifest.files(plan.agent)
    failed = {p for item in plan.items if item.status == "error" for p in item.sources}
    for path in plan.files():
        if path not in failed:
            files[str(path)] = file_digest(path)
    skills = manifest.skills(plan.agent)
    for item in plan.items:
        if item.skill is not None and item.outcome == "done":
            name, target = item.skill
            skills[name] = tree_digest(target)
    entry.update(
        {
            "source": str(plan.source),
            "project": str(plan.project) if plan.project is not None else None,
            "overwrite": overwrite,
            "imported_at": stamp,
            "files": dict(sorted(files.items())),
            "skills": dict(sorted(skills.items())),
        }
    )
    manifest.agents[plan.agent] = entry
    manifest.write(home)
    return manifest


def changed_items(plan: Plan, manifest: Manifest) -> list[Item]:
    """The items whose source files changed since the recorded import (``--sync``)."""
    recorded = manifest.files(plan.agent)
    out: list[Item] = []
    for item in plan.items:
        if not item.sources:
            continue
        if any(recorded.get(str(p)) != file_digest(p) for p in item.sources):
            out.append(item)
    return out
