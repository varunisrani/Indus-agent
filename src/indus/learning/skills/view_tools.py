"""``skills_list`` and ``skill_view``: progressive disclosure of skills (P09-08).

Level 0 — ``skills_list``: one row per visible skill (``name``, ``description``, ``category``),
cached per profile for ``LIST_CACHE_TTL_S`` on the scope's clock (``clear_list_cache`` drops it
after a write). Level 1 — ``skill_view(name)``: the full ``SKILL.md``, its parsed frontmatter and
the support files it ships. Level 2 — ``skill_view(name, file_path)``: one support file, which
must stay inside the skill folder after symlinks are resolved.

Required environment variables: missing ones are asked for through the session's server-request
broker (``secret`` kind) only when the run is interactive; a client that cannot answer secrets
(messaging) or a cron / one-shot / unattended run gets a setup hint instead. Declared names join
the passthrough set for sandboxed child processes.

Each view bumps the usage counters and records read evidence per (session, skill, file) with the
hash of what was shown; the review fork's write guard in ``skill_manage`` checks it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.learning.skills.format import LAYOUT_DIRS, SKILL_FILE, VisibilityContext
from indus.learning.skills.resolver import SkillError, SkillRecord, load_catalog
from indus.learning.skills.usage import usage_for
from indus.protocol.requests_port import RequestNotAnswered, RequestUnsupported
from indus.safety.child_env import register_passthrough
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "LIST_CACHE_TTL_S",
    "READS",
    "SKILLS_LIST_SCHEMA",
    "SKILL_VIEW_SCHEMA",
    "TOOLSET",
    "ReadEvidence",
    "clear_list_cache",
    "linked_files",
    "register_tools",
    "skill_view",
    "skills_list",
]

log = logging.getLogger(__name__)

TOOLSET = "skills"
LIST_CACHE_TTL_S = 30.0
MAX_FILE_BYTES = 1024 * 1024
NO_PROMPT_CONTEXTS = frozenset({"cron", "single_query", "unattended"})

SKILLS_LIST_SCHEMA: dict[str, Any] = {
    "description": (
        "List the skills available in this profile: name, short description and category. "
        "Open one with skill_view(name) to read its full procedure."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Only list skills in this category.",
            }
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA: dict[str, Any] = {
    "description": (
        "Open a skill. With only name: returns its SKILL.md, metadata and the list of support "
        "files. With file_path (for example references/api.md): returns that one support file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name, or its path when several skills share a name.",
            },
            "file_path": {
                "type": "string",
                "description": "A support file inside the skill folder, relative to it.",
            },
        },
        "required": ["name"],
    },
}


# -- read evidence --------------------------------------------------------------------------------


def content_hash(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


class ReadEvidence:
    """What each session has viewed: (session, skill, file) → hash of the content shown."""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def mark(self, session_id: str | None, skill: str, file: str, digest: str) -> None:
        with self._lock:
            self._seen[(session_id or "", skill, file)] = digest

    def fresh(self, session_id: str | None, skill: str, file: str, current: bytes | None) -> bool:
        """True when the session viewed exactly ``current`` (None = the file does not exist)."""
        with self._lock:
            seen = self._seen.get((session_id or "", skill, file))
        if seen is None:
            return False
        return current is not None and seen == content_hash(current)

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._seen.clear()
            else:
                for key in [k for k in self._seen if k[0] == session_id]:
                    del self._seen[key]


READS = ReadEvidence()


# -- list -------------------------------------------------------------------------------------

_LIST_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_LIST_LOCK = threading.Lock()


def clear_list_cache(scope: Scope | None = None) -> None:
    with _LIST_LOCK:
        if scope is None:
            _LIST_CACHE.clear()
        else:
            _LIST_CACHE.pop(str(scope.home), None)


def _list_visibility() -> VisibilityContext:
    return VisibilityContext(tools_known=False)


def _row(rec: SkillRecord) -> dict[str, Any]:
    return {
        "name": rec.load_name or rec.name,
        "description": rec.description,
        "category": rec.category,
    }


def _list_rows(scope: Scope) -> list[dict[str, Any]]:
    key = str(scope.home)
    now = scope.clock.monotonic()
    with _LIST_LOCK:
        hit = _LIST_CACHE.get(key)
        if hit is not None and now - hit[0] < LIST_CACHE_TTL_S:
            return hit[1]
    rows = [_row(r) for r in load_catalog(scope).visible(_list_visibility())]
    with _LIST_LOCK:
        _LIST_CACHE[key] = (now, rows)
    return rows


def skills_list(args: Mapping[str, Any], *, scope: Scope | None = None) -> str:
    if scope is None:
        return ToolResult.error("skills_list needs a profile scope").text
    rows = _list_rows(scope)
    category = args.get("category")
    if isinstance(category, str) and category.strip():
        rows = [r for r in rows if r["category"] == category.strip()]
    payload: dict[str, Any] = {"skills": rows, "count": len(rows)}
    if not rows:
        payload["hint"] = "No skills match. skill_manage(action='create') can save a new one."
    return json.dumps(payload, ensure_ascii=False)


# -- view ---------------------------------------------------------------------------------------


def linked_files(skill_dir: Path) -> dict[str, list[str]]:
    """Support files by folder (relative posix paths, files only; empty folders left out)."""
    out: dict[str, list[str]] = {}
    for folder in LAYOUT_DIRS:
        base = skill_dir / folder
        if not base.is_dir():
            continue
        found = sorted(
            p.relative_to(skill_dir).as_posix()
            for p in base.rglob("*")
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(base).parts)
        )
        if found:
            out[folder] = found
    return out


def _inside(path: Path, folder: Path) -> bool:
    real = os.path.realpath(path)
    base = os.path.realpath(folder)
    return real == base or real.startswith(base + os.sep)


def _bump_view(scope: Scope, name: str) -> None:
    try:
        usage_for(scope).bump_view(name)
    except Exception:
        log.warning("view count for %s not recorded", name, exc_info=True)


def _view_file(scope: Scope, rec: SkillRecord, file_path: str, session_id: str | None) -> str:
    rel = file_path.strip().replace("\\", "/")
    target = rec.skill_dir / rel
    if (
        not rel
        or Path(rel).is_absolute()
        or ".." in rel.split("/")
        or not _inside(target, rec.skill_dir)
    ):
        return ToolResult.error(
            f"file_path {file_path!r} is outside the skill folder of {rec.name!r}"
        ).text
    if not target.is_file():
        files = [f for group in linked_files(rec.skill_dir).values() for f in group]
        return ToolResult.error(f"{rec.name!r} has no file {rel!r}", available_files=files).text
    try:
        data = target.read_bytes()
    except OSError as exc:
        return ToolResult.error(f"could not read {rel!r}: {exc.strerror or exc}").text
    READS.mark(session_id, rec.name, rel, content_hash(data))
    _bump_view(scope, rec.name)
    payload: dict[str, Any] = {"name": rec.name, "file_path": rel}
    shown = data[:MAX_FILE_BYTES]
    try:
        payload["content"] = shown.decode("utf-8")
    except UnicodeDecodeError:
        payload["binary"] = True
        payload["size"] = len(data)
        payload["note"] = "binary file; content not shown"
        return json.dumps(payload, ensure_ascii=False)
    if len(data) > MAX_FILE_BYTES:
        payload["note"] = f"only the first {MAX_FILE_BYTES:,} of {len(data):,} bytes are shown"
    return json.dumps(payload, ensure_ascii=False)


def _has_value(scope: Scope, name: str) -> bool:
    if scope.env.get(name):
        return True
    secrets = scope.secrets
    try:
        return bool(secrets.get(name)) if secrets is not None else False
    except Exception:
        return False


def _setup_hint(names: list[str]) -> str:
    listed = ", ".join(names)
    return (
        f"This skill needs {listed}, which is not set. Run Indus in a local terminal and view the "
        "skill there to enter it, or store it with `indus config set "
        f"{names[0]} <value>`; then try again."
    )


async def _ensure_env(
    scope: Scope, rec: SkillRecord, broker: Any, run_context: str | None
) -> dict[str, Any] | None:
    fm = rec.frontmatter
    if fm is None or not fm.required_environment_variables:
        return None
    declared = [v.name for v in fm.required_environment_variables]
    register_passthrough(declared)
    missing = [v for v in fm.required_environment_variables if not _has_value(scope, v.name)]
    if not missing:
        return None
    if broker is None or run_context in NO_PROMPT_CONTEXTS:
        return {
            "missing": [v.name for v in missing],
            "hint": _setup_hint([v.name for v in missing]),
        }
    still: list[str] = []
    saver = getattr(scope.secrets, "set", None)
    for var in missing:
        prompt = var.prompt or f"Value for {var.name} (needed by the {rec.name} skill)"
        if var.help:
            prompt = f"{prompt}\n{var.help}"
        try:
            answer = await broker.ask("secret", {"name": var.name, "prompt": prompt})
        except RequestUnsupported:
            return {
                "missing": [v.name for v in missing],
                "hint": _setup_hint([v.name for v in missing]),
            }
        except RequestNotAnswered:
            still.append(var.name)
            continue
        value = getattr(answer, "value", None)
        if not value or saver is None:
            still.append(var.name)
            continue
        try:
            saver(var.name, value)
        except Exception as exc:
            log.warning("could not store %s for skill %s: %s", var.name, rec.name, exc)
            still.append(var.name)
    if not still:
        return {"stored": [v.name for v in missing]}
    return {"missing": still, "hint": _setup_hint(still)}


async def skill_view(
    args: Mapping[str, Any],
    *,
    scope: Scope | None = None,
    session_id: str | None = None,
    broker: Any = None,
    run_context: str | None = None,
) -> str:
    if scope is None:
        return ToolResult.error("skill_view needs a profile scope").text
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return ToolResult.error("name is required").text
    try:
        rec = load_catalog(scope).resolve(name)
    except SkillError as exc:
        return ToolResult.error(str(exc)).text
    file_path = args.get("file_path")
    if isinstance(file_path, str) and file_path.strip() and file_path.strip() != SKILL_FILE:
        return _view_file(scope, rec, file_path, session_id)
    try:
        data = rec.skill_md.read_bytes()
    except OSError as exc:
        return ToolResult.error(f"could not read {rec.name!r}: {exc.strerror or exc}").text
    text = data.decode("utf-8", errors="replace")
    READS.mark(session_id, rec.name, SKILL_FILE, content_hash(data))
    _bump_view(scope, rec.name)
    fm = rec.frontmatter
    payload: dict[str, Any] = {
        "name": rec.name,
        "path": str(rec.skill_dir),
        "content": text,
        "metadata": fm.model_dump(mode="json", exclude_none=True) if fm is not None else {},
        "linked_files": linked_files(rec.skill_dir),
    }
    setup = await _ensure_env(scope, rec, broker, run_context)
    if setup is not None:
        payload["setup"] = setup
    return json.dumps(payload, ensure_ascii=False)


def register_tools(registry: ToolRegistry) -> None:
    traits = ToolTraits(read_only=True)
    registry.register("skills_list", TOOLSET, SKILLS_LIST_SCHEMA, skills_list, traits=traits)
    registry.register("skill_view", TOOLSET, SKILL_VIEW_SCHEMA, skill_view, traits=traits)
