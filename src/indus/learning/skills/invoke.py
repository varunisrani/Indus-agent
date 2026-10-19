"""Skills as slash commands, preprocessing, and auto-load (P09-09).

``/skill-a /skill-b do the thing`` becomes one new user message: a frame naming what was run, each
skill's body in a ``<skill>`` block, a skill-config block for settings the skill declares, then the
user's instruction. Up to ``MAX_STACKED`` leading skill words are taken; the first word that is not
an installed skill (or repeats one) ends the stack and starts the instruction. The system prompt is
never touched.

Preprocessing of each body: ``${INDUS_SKILL_DIR}`` / ``${INDUS_SESSION_ID}`` are replaced when
``skills.template_vars`` is on (default); ``!`command``` snippets run only when
``skills.inline_shell`` is on (default off), each with a ``INLINE_SHELL_TIMEOUT_S`` limit, in the
skill folder, with the child environment from ``safety.child_env``, and never for community hub
skills.

``skills.auto_load`` (and the ``-s/--skill`` preload list) load named skills into a block appended
to the prompt when it is first built; missing names warn once and are skipped, and
``INDUS_IGNORE_RULES`` turns auto-load off.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from indus.foundation.scope import Scope
from indus.learning.skills.format import ConfigVar, VisibilityContext, split_frontmatter
from indus.learning.skills.resolver import (
    SkillCatalog,
    SkillError,
    SkillRecord,
    load_catalog,
    skills_setting,
)
from indus.learning.skills.usage import HUB_LOCK, usage_for
from indus.learning.skills.view_tools import linked_files
from indus.platform.locate import which
from indus.safety.child_env import child_env

__all__ = [
    "INLINE_SHELL_TIMEOUT_S",
    "MAX_STACKED",
    "CommandTable",
    "Expansion",
    "ShellRunner",
    "auto_load_block",
    "expand_skills",
    "is_community_skill",
    "preprocess",
    "skill_command_names",
    "split_stack",
]

log = logging.getLogger(__name__)

MAX_STACKED = 5
INLINE_SHELL_TIMEOUT_S = 10.0
INLINE_OUTPUT_MAX = 4000
IGNORE_RULES_ENV = "INDUS_IGNORE_RULES"

_TEMPLATE = re.compile(r"\$\{(INDUS_SKILL_DIR|INDUS_SESSION_ID)\}")
_INLINE = re.compile(r"!`([^`\n]+)`")

ShellRunner = Callable[[str, Path, float], str]
"""``(command, working folder, timeout seconds) -> output``; raises ``TimeoutError`` on time-out."""

FRAME_ONE = (
    "[The user ran the /{names} skill. It is loaded in full below; follow it for this request.]"
)
FRAME_MANY = (
    "[The user ran these skills together: {names}. They are loaded in full below; follow them "
    "for this request.]"
)
INSTRUCTION = "User instruction: {text}"
NO_INSTRUCTION = (
    "User instruction: none given. Apply the skill to the conversation so far, or ask what the "
    "user needs."
)
MISSING_NOTE = "[Not loaded (not installed or not available here): {names}]"
AUTO_LOAD_HEADING = "## Loaded skills"
AUTO_LOAD_LEAD = (
    "These skills are loaded for the whole session ({source}); follow them wherever they apply."
)

_WARNED: set[tuple[str, str]] = set()
_WARNED_LOCK = threading.Lock()


@dataclass(frozen=True)
class Expansion:
    text: str
    loaded: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


# -- parsing ------------------------------------------------------------------------------------


def split_stack(
    text: str, is_skill: Callable[[str], bool], *, limit: int = MAX_STACKED
) -> tuple[list[str], str] | None:
    """``(skill names, instruction)`` for leading ``/skill`` words, or None when there are none."""
    names: list[str] = []
    rest = text.lstrip()
    while len(names) < limit and rest.startswith("/"):
        word = rest.split(None, 1)[0]
        name = word[1:].lower()
        if not name or "/" in name or name in names or not is_skill(name):
            break
        names.append(name)
        rest = rest[len(word) :].lstrip()
    if not names:
        return None
    return names, rest.strip()


def skill_command_names(
    catalog: SkillCatalog, ctx: VisibilityContext | None = None
) -> dict[str, str]:
    """``{command: description}`` for each skill a slash word can run (not ambiguous names)."""
    out: dict[str, str] = {}
    for rec in catalog.visible(ctx):
        if rec.status != "unique":
            continue
        out.setdefault(rec.name.lower(), rec.description)
    return out


# -- preprocessing ------------------------------------------------------------------------------


def is_community_skill(scope: Scope, rec: SkillRecord) -> bool:
    """True for a hub skill whose lock row says ``trust: community`` (P13 owns the hub)."""
    try:
        data = json.loads((scope.home / "skills" / HUB_LOCK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if isinstance(data, dict) and isinstance(data.get("installed"), dict):
        data = data["installed"]
    row = data.get(rec.name) if isinstance(data, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return isinstance(row, dict) and row.get("trust") == "community"  # pyright: ignore[reportUnknownMemberType]


def default_runner(scope: Scope) -> ShellRunner:
    def run(command: str, cwd: Path, timeout: float) -> str:
        bash = which("bash") or "bash"
        try:
            done = subprocess.run(
                [bash, "-c", command],
                cwd=str(cwd),
                env=child_env(scope),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(command) from exc
        output = (done.stdout or "").rstrip("\n") or (done.stderr or "").rstrip("\n")
        if done.returncode != 0 and not output:
            return f"[inline command exited {done.returncode} with no output: {command}]"
        return output

    return run


def _run_snippet(runner: ShellRunner, command: str, cwd: Path) -> str:
    try:
        output = runner(command, cwd, INLINE_SHELL_TIMEOUT_S)
    except TimeoutError:
        return f"[inline command timed out after {INLINE_SHELL_TIMEOUT_S:g} s: {command}]"
    except FileNotFoundError:
        return "[inline command not run: bash was not found]"
    except Exception as exc:
        return f"[inline command failed: {type(exc).__name__}: {exc}]"
    if len(output) > INLINE_OUTPUT_MAX:
        output = output[:INLINE_OUTPUT_MAX] + " [output cut]"
    return output


def preprocess(
    text: str,
    *,
    skill_dir: Path,
    session_id: str | None,
    template_vars: bool = True,
    inline_shell: bool = False,
    runner: ShellRunner | None = None,
) -> str:
    """Apply template tokens and (when allowed) inline shell snippets to a skill body."""
    out = text
    if template_vars:
        values = {"INDUS_SKILL_DIR": str(skill_dir), "INDUS_SESSION_ID": session_id or ""}
        out = _TEMPLATE.sub(lambda m: values[m.group(1)] or m.group(0), out)
    if inline_shell and runner is not None and "!`" in out:
        out = _INLINE.sub(
            lambda m: (
                _run_snippet(runner, m.group(1).strip(), skill_dir) if m.group(1).strip() else ""
            ),
            out,
        )
    return out


# -- loading ------------------------------------------------------------------------------------


def _config_lines(scope: Scope, declared: Sequence[ConfigVar]) -> list[str]:
    lines: list[str] = []
    for var in declared:
        value = skills_setting(scope, f"skills.config.{var.key}")
        if value is None:
            value = var.default
        if value is None:
            continue
        lines.append(f"- {var.key}: {value}")
    return lines


@dataclass
class _Loader:
    scope: Scope
    session_id: str | None
    runner: ShellRunner | None = None
    template_vars: bool = True
    inline_shell: bool = False
    used: list[str] = field(default_factory=list[str])

    @classmethod
    def for_scope(cls, scope: Scope, session_id: str | None, runner: ShellRunner | None) -> _Loader:
        inline = skills_setting(scope, "skills.inline_shell") is True
        return cls(
            scope=scope,
            session_id=session_id,
            runner=runner if runner is not None else (default_runner(scope) if inline else None),
            template_vars=skills_setting(scope, "skills.template_vars") is not False,
            inline_shell=inline,
        )

    def block(self, rec: SkillRecord) -> str:
        raw = rec.skill_md.read_text(encoding="utf-8")
        _, body = split_frontmatter(raw)
        body = preprocess(
            body.strip(),
            skill_dir=rec.skill_dir,
            session_id=self.session_id,
            template_vars=self.template_vars,
            inline_shell=self.inline_shell and not is_community_skill(self.scope, rec),
            runner=self.runner,
        )
        parts = [f'<skill name="{rec.name}" path="{rec.skill_dir}">', body]
        files = [f for group in linked_files(rec.skill_dir).values() for f in group]
        if files:
            parts.append(
                "Support files (open with skill_view(name, file_path)): " + ", ".join(files)
            )
        parts.append("</skill>")
        fm = rec.frontmatter
        config = _config_lines(self.scope, fm.indus.config) if fm is not None else []
        text = "\n".join(parts)
        if config:
            text += f"\n[Skill config for {rec.name}:\n" + "\n".join(config) + "\n]"
        self.used.append(rec.name)
        return text

    def bump(self) -> None:
        usage = usage_for(self.scope)
        for name in self.used:
            usage.bump_use(name)


def _load_blocks(
    catalog: SkillCatalog, names: Iterable[str], loader: _Loader
) -> tuple[list[str], list[str], list[str]]:
    blocks: list[str] = []
    loaded: list[str] = []
    missing: list[str] = []
    for name in names:
        try:
            rec = catalog.resolve(name)
            blocks.append(loader.block(rec))
        except (SkillError, OSError, UnicodeDecodeError) as exc:
            log.info("skill %s not loaded: %s", name, exc)
            missing.append(name)
            continue
        loaded.append(rec.name)
    return blocks, loaded, missing


def expand_skills(
    names: Sequence[str],
    instruction: str,
    *,
    scope: Scope,
    session_id: str | None = None,
    catalog: SkillCatalog | None = None,
    runner: ShellRunner | None = None,
    frame: str | None = None,
    instruction_first: bool = False,
) -> Expansion | None:
    """One user message loading ``names``; None when none of them could be loaded."""
    cat = catalog if catalog is not None else load_catalog(scope)
    loader = _Loader.for_scope(scope, session_id, runner)
    blocks, loaded, missing = _load_blocks(cat, names, loader)
    if not blocks:
        return None
    if frame is None:
        frame = (
            FRAME_ONE.format(names=loaded[0])
            if len(loaded) == 1
            else FRAME_MANY.format(names=", ".join(f"/{n}" for n in loaded))
        )
    said = INSTRUCTION.format(text=instruction.strip()) if instruction.strip() else NO_INSTRUCTION
    head = [frame]
    if instruction_first:
        head.append(said)
    if missing:
        head.append(MISSING_NOTE.format(names=", ".join(missing)))
    pieces = ["\n".join(head), *blocks]
    if not instruction_first:
        pieces.append(said)
    loader.bump()
    return Expansion(text="\n\n".join(pieces), loaded=tuple(loaded), missing=tuple(missing))


# -- auto-load ----------------------------------------------------------------------------------


def _warn_missing_once(scope: Scope, name: str, source: str) -> None:
    key = (str(scope.home), f"{source}:{name}")
    with _WARNED_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    log.warning("%s skill %r is not installed or not available; skipped", source, name)


def auto_load_block(
    scope: Scope,
    *,
    session_id: str | None = None,
    extra: Sequence[str] = (),
    catalog: SkillCatalog | None = None,
    runner: ShellRunner | None = None,
) -> str:
    """The prompt block for ``skills.auto_load`` plus ``extra`` (the ``-s`` preload list)."""
    configured: list[str] = []
    if not scope.env.get(IGNORE_RULES_ENV):
        raw = skills_setting(scope, "skills.auto_load")
        if isinstance(raw, list):
            configured = [str(n).strip() for n in raw if str(n).strip()]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    wanted: list[str] = []
    for name in [*configured, *extra]:
        if name not in wanted:
            wanted.append(name)
    if not wanted:
        return ""
    cat = catalog if catalog is not None else load_catalog(scope)
    loader = _Loader.for_scope(scope, session_id, runner)
    blocks, _loaded, missing = _load_blocks(cat, wanted, loader)
    for name in missing:
        _warn_missing_once(scope, name, "auto-load" if name in configured else "preload")
    if not blocks:
        return ""
    loader.bump()
    source = "skills.auto_load" if not extra else "loaded at start"
    return "\n\n".join([AUTO_LOAD_HEADING, AUTO_LOAD_LEAD.format(source=source), *blocks])


def reset_warnings() -> None:
    with _WARNED_LOCK:
        _WARNED.clear()


# -- the command table --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandTable:
    """Runnable skill and bundle commands for one surface; a bundle wins over a same-slug skill.

    ``bundles`` maps a slug to an expander ``(instruction) -> Expansion | None`` (P09-10).
    """

    skills: Mapping[str, str]
    bundles: Mapping[str, Callable[[str], Expansion | None]] = field(
        default_factory=dict[str, Callable[[str], Expansion | None]]
    )

    def names(self) -> dict[str, str]:
        out = {name: f"skill: {desc}" if desc else "skill" for name, desc in self.skills.items()}
        out.update({slug: "skill bundle" for slug in self.bundles})
        return out

    def run(
        self,
        text: str,
        *,
        scope: Scope,
        session_id: str | None = None,
        catalog: SkillCatalog | None = None,
        runner: ShellRunner | None = None,
    ) -> Expansion | None:
        """The expansion for slash ``text``, or None when its first word is not ours."""
        stripped = text.lstrip()
        first = stripped.split(None, 1)[0][1:].lower() if stripped.startswith("/") else ""
        if first in self.bundles:
            rest = stripped[len(first) + 1 :].strip()
            return self.bundles[first](rest)
        split = split_stack(stripped, lambda n: n in self.skills and n not in self.bundles)
        if split is None:
            return None
        names, instruction = split
        return expand_skills(
            names, instruction, scope=scope, session_id=session_id, catalog=catalog, runner=runner
        )
