"""The tiered system-prompt builder (P04-05).

``PromptBuilder.build(ctx)`` renders three tiers, joined by blank lines, ordered from the text least
likely to change to the most likely, so a longest-prefix provider cache keeps as much as it can:

- **stable**: identity (persona or the built-in one), the product-help pointer, tool guidance
  (only with tools; each block behind its config switch or model-family gate) and, when there is
  no workspace snapshot, the workspace guidance and the platform hint;
- **context**: the caller's system text, project context files, then anything naming the current
  worktree (the snapshot and the trailing guidance after it);
- **volatile**: memory snapshot, user-profile snapshot, the external memory provider's block, the
  plugin sections (P13-18, frozen per conversation), skills index (P09-04 order), the
  active-profile line, the date block and finally the runtime block between ``RUNTIME_HEADING``
  and ``RUNTIME_END``.

The prompt is built once per conversation; ``lifecycle.PromptLifecycle`` decides when. The date
block carries no time of day so a prompt built in the morning is still the same bytes at night.
"""

from __future__ import annotations

import logging
import platform as host_platform
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from indus.core.prompt import texts
from indus.core.prompt.context_files import (
    CAP_KEY,
    Reader,
    ReadSettings,
    collect_warnings,
    context_cap,
    install_root,
    load_project_context,
)
from indus.core.prompt.persona import load_persona
from indus.foundation.clock import resolve_zone, safe_strftime
from indus.foundation.profiles import DEFAULT_PROFILE
from indus.foundation.scope import Scope
from indus.platform import facts

__all__ = [
    "ACT_FAMILIES",
    "DISCIPLINE_FAMILIES",
    "GOOGLE_FAMILIES",
    "BuildContext",
    "PromptBuilder",
    "PromptParts",
    "RuntimeInfo",
    "configured_hints",
    "date_block",
    "execution_text",
    "join_parts",
    "local_runtime",
    "model_gate",
    "platform_hint",
    "runtime_block",
]

log = logging.getLogger(__name__)

# Model-name substrings that get the "act with your tools" block under ``auto`` (D-16.66, ours).
ACT_FAMILIES = ("gpt", "codex", "gemini", "gemma", "grok", "qwen", "glm", "deepseek", "kimi")
# Substrings that get the execution-discipline block under ``auto``.
DISCIPLINE_FAMILIES = (
    "gpt",
    "codex",
    "grok",
    "deepseek",
    "kimi",
    "qwen",
    "glm",
    "minimax",
    "mistral",
)
# Families that also get the Google working-style block when the act gate is on.
GOOGLE_FAMILIES = ("gemini", "gemma")

TASK_COMPLETION_KEY = "prompt.task_completion_guidance"
PARALLEL_TOOLS_KEY = "prompt.parallel_tool_guidance"
ACT_GATE_KEY = "prompt.tool_use_enforcement"
DISCIPLINE_GATE_KEY = "prompt.execution_guidance"
PLATFORM_HINTS_KEY = "prompt.platform_hints"

_ON_WORDS = frozenset({"true", "yes", "on", "always"})
_OFF_WORDS = frozenset({"false", "no", "off", "never"})


@dataclass(frozen=True)
class RuntimeInfo:
    """Where the agent's tools run. ``backend`` (a remote or sandbox description) replaces the
    host lines; ``notes`` are extra paragraphs (for example the WSL hint)."""

    host: str = ""
    home: str = ""
    cwd: str = ""
    backend: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildContext:
    """Everything one build needs. Model, provider and platform come from the bound route and
    surface; the snapshot slots stay empty until the phases that fill them (P09 memory/skills)."""

    scope: Scope
    model: str = ""
    provider: str = ""
    platform: str = ""
    session_id: str = ""
    show_session_id: bool = False
    tool_names: frozenset[str] = frozenset()
    system_message: str | None = None
    cwd: Path | None = None
    context_length: int | None = None
    context_files: bool = True
    persona_without_context_files: bool = False
    workspace_snapshot: str = ""
    skills_index: str = ""
    memory_snapshot: str = ""
    profile_snapshot: str = ""
    # The external memory provider's static block, frozen for the session (P13-24); "" = none.
    memory_provider_block: str = ""
    # The plugin sections block rendered once for the conversation (P13-18); "" = none.
    plugin_sections: str = ""
    # The memory tool's guidance for this session (P09-04): None = the stock text, "" = none.
    memory_guidance: str | None = None
    started_at: datetime | None = None
    runtime: RuntimeInfo | None = None

    def working_dir(self) -> Path:
        return self.cwd if self.cwd is not None else Path.cwd()


@dataclass(frozen=True)
class PromptParts:
    stable: str
    context: str
    volatile: str
    text: str
    warnings: tuple[str, ...] = ()
    persona_loaded: bool = False


def _setting(scope: Scope, key: str) -> object:
    config = scope.config
    if config is None:
        return None
    try:
        return config.get(key)
    except Exception:  # a broken config view must never break prompt assembly
        log.debug("config read failed for %s", key, exc_info=True)
        return None


def _switch(scope: Scope, key: str, default: bool = True) -> bool:
    value = _setting(scope, key)
    return value if isinstance(value, bool) else default


def join_parts(parts: Sequence[str | None]) -> str:
    """Non-blank parts, each stripped, joined by one blank line."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


# -- gates and hints ------------------------------------------------------------------------------


def model_gate(setting: object, model: str, families: Sequence[str]) -> bool:
    """A guidance gate: bool or on/off word forces it, a list of substrings matches the model
    name (case-insensitive), anything else (``auto``) matches ``families``."""
    if isinstance(setting, bool):
        return setting
    name = model.lower()
    if isinstance(setting, str):
        word = setting.strip().lower()
        if word in _ON_WORDS:
            return True
        if word in _OFF_WORDS:
            return False
    if isinstance(setting, (list, tuple)):
        items = cast(Sequence[object], setting)
        return any(isinstance(p, str) and p.strip() and p.strip().lower() in name for p in items)
    return any(family in name for family in families)


def execution_text(tool_names: frozenset[str]) -> str:
    """The execution-discipline block with only the lines whose tools are loaded."""
    lines = [line for line, tools in texts.EXECUTION_DISCIPLINE_LINES if tools & tool_names]
    body = texts.EXECUTION_DISCIPLINE_INTRO + "".join(lines) + "\n" if lines else ""
    return texts.EXECUTION_DISCIPLINE_TITLE + body + texts.EXECUTION_DISCIPLINE_TAIL


def configured_hints(scope: Scope) -> Mapping[str, object]:
    value = _setting(scope, PLATFORM_HINTS_KEY)
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def platform_hint(platform: str, overrides: Mapping[str, object] | None = None) -> str:
    """The built-in hint for ``platform`` with the config override applied.

    A bare string appends; a mapping may ``replace`` the built-in text and/or ``append`` to it
    (both apply: replacement first, then the addition). Anything malformed leaves the built-in
    hint as it is, so a bad setting can never break the prompt.
    """
    key = platform.strip().lower()
    base = texts.PLATFORM_HINTS.get(key, "")
    spec: object = overrides.get(key) if overrides and key else None
    if isinstance(spec, str):
        spec = {"append": spec}
    if not isinstance(spec, Mapping):
        return base
    entry = cast(Mapping[str, object], spec)
    replace = entry.get("replace")
    append = entry.get("append")
    if (replace is not None and not isinstance(replace, str)) or (
        append is not None and not isinstance(append, str)
    ):
        log.warning("prompt.platform_hints.%s is malformed; using the built-in hint", key)
        return base
    text = replace.strip() if replace and replace.strip() else base
    if append and append.strip():
        text = f"{text}\n\n{append.strip()}".strip()
    return text


def _help_pointer(tool_names: frozenset[str], skills_index: str) -> str:
    listed = f"- {texts.HELP_SKILL_NAME}:" in skills_index
    if "skill_view" in tool_names and listed:
        return texts.HELP_POINTER_WITH_SKILL
    return texts.HELP_POINTER


def _tool_guidance(tool_names: frozenset[str], memory: str | None = None) -> list[str]:
    guidance: list[tuple[str, str]] = [
        ("memory", texts.MEMORY_GUIDANCE if memory is None else memory),
        ("session_search", texts.SESSION_SEARCH_GUIDANCE),
        ("skill_manage", texts.SKILL_WRITING_GUIDANCE),
    ]
    return [text for tool, text in guidance if tool in tool_names and text.strip()]


# -- volatile pieces ------------------------------------------------------------------------------


def _active_profile_line(scope: Scope) -> str:
    if scope.profile_id == DEFAULT_PROFILE:
        return texts.ACTIVE_PROFILE_DEFAULT.format(home=scope.home, root=scope.root)
    return texts.ACTIVE_PROFILE_NAMED.format(
        name=scope.profile_id, home=scope.home, root=scope.root
    )


def _in_zone(moment: datetime, zone: tzinfo | None) -> datetime:
    if zone is not None:
        return moment.astimezone(zone)
    return moment.astimezone()


def _zone_bits(now: datetime, zone: tzinfo | None) -> list[str]:
    key = zone.key if isinstance(zone, ZoneInfo) else ""
    bits = [key] if key else []
    abbreviation = safe_strftime(now, "%Z")
    if abbreviation and abbreviation != key:
        bits.append(abbreviation)
    offset = safe_strftime(now, "%z")
    if len(offset) >= 5:
        bits.append(f"UTC{offset[:3]}:{offset[3:5]}")
    return bits


def _day(moment: datetime) -> str:
    return safe_strftime(moment, "%A, %Y-%m-%d")


def date_block(ctx: BuildContext) -> str:
    """Conversation date (no time of day), a rebuild-date line on a later day, identity lines."""
    zone = resolve_zone(ctx.scope)
    now = _in_zone(ctx.scope.clock.now(), zone)
    start = _in_zone(ctx.started_at, zone) if ctx.started_at is not None else now
    bits = _zone_bits(now, zone)
    lines = [texts.DATE_LINE.format(date=_day(start), zone=f" ({', '.join(bits)})" if bits else "")]
    if now.date() != start.date():
        lines.append(texts.REBUILD_DATE_LINE.format(date=_day(now)))
    trailer = (
        (texts.LABEL_SESSION_ID, ctx.session_id if ctx.show_session_id else ""),
        (texts.LABEL_MODEL, ctx.model),
        (texts.LABEL_PROVIDER, ctx.provider),
        (texts.LABEL_PLATFORM, ctx.platform),
    )
    lines.extend(f"{label}: {value.strip()}" for label, value in trailer if value.strip())
    return "\n".join(lines)


def _host_description() -> str:
    if facts.is_wsl():
        return f"Linux under WSL ({host_platform.release()})"
    if sys.platform == "darwin":
        return f"macOS {host_platform.mac_ver()[0] or host_platform.release()}"
    if sys.platform == "win32":
        return f"Windows {host_platform.release()} ({host_platform.version()})"
    return f"{host_platform.system()} {host_platform.release()}".strip()


def local_runtime(cwd: Path, env: Mapping[str, str] | None = None) -> RuntimeInfo:
    """Runtime facts for the local terminal backend: the tools run on this host."""
    variables = env or {}
    home = variables.get("HOME") or variables.get("USERPROFILE") or str(Path.home())
    notes = (texts.WSL_HINT,) if facts.is_wsl() else ()
    return RuntimeInfo(host=_host_description(), home=home, cwd=str(cwd), notes=notes)


def _defuse(text: str) -> str:
    # Only the renderer may write the runtime heading and end marker.
    return text.replace(texts.RUNTIME_HEADING, "> " + texts.RUNTIME_HEADING).replace(
        texts.RUNTIME_END, "> " + texts.RUNTIME_END
    )


def runtime_block(info: RuntimeInfo) -> str:
    if info.backend.strip():
        first = info.backend.strip()
    else:
        rows = (
            (texts.LABEL_HOST, info.host),
            (texts.LABEL_HOME, info.home),
            (texts.LABEL_CWD, info.cwd),
        )
        first = "\n".join(f"{label}: {value}" for label, value in rows if value)
    body = join_parts([_defuse(first), *(_defuse(note) for note in info.notes)])
    return f"{texts.RUNTIME_HEADING}\n\n{body}\n\n{texts.RUNTIME_END}"


# -- the builder ----------------------------------------------------------------------------------


@dataclass
class PromptBuilder:
    """Builds ``PromptParts``. ``reader`` and ``install`` are injectable for tests."""

    reader: Reader | None = None
    install: Path | None = field(default_factory=install_root)

    async def build(self, ctx: BuildContext) -> PromptParts:
        with collect_warnings() as warnings:
            parts = await self._render(ctx)
            return PromptParts(
                stable=parts[0],
                context=parts[1],
                volatile=parts[2],
                text=join_parts(parts),
                warnings=tuple(warnings),
                persona_loaded=parts[3] == "persona",
            )

    async def _render(self, ctx: BuildContext) -> tuple[str, str, str, str]:
        scope = ctx.scope
        tools = ctx.tool_names
        reads = ReadSettings.for_scope(scope, self.reader)
        cap = context_cap(ctx.context_length, _setting(scope, CAP_KEY))

        persona: str | None = None
        if ctx.context_files or ctx.persona_without_context_files:
            persona = await load_persona(scope.home, reads=reads, cap=cap)

        identity = persona or texts.DEFAULT_IDENTITY
        stable: list[str] = [identity, _help_pointer(tools, ctx.skills_index)]
        if tools:
            if _switch(scope, TASK_COMPLETION_KEY):
                stable.append(texts.TASK_COMPLETION)
            if _switch(scope, PARALLEL_TOOLS_KEY):
                stable.append(texts.PARALLEL_TOOLS)
            stable.extend(_tool_guidance(tools, ctx.memory_guidance))
            stable.append(texts.STEER_NOTE)
            if model_gate(_setting(scope, ACT_GATE_KEY), ctx.model, ACT_FAMILIES):
                stable.append(texts.ACT_GUIDANCE)
                if any(family in ctx.model.lower() for family in GOOGLE_FAMILIES):
                    stable.append(texts.GOOGLE_FAMILY_GUIDANCE)
            if model_gate(_setting(scope, DISCIPLINE_GATE_KEY), ctx.model, DISCIPLINE_FAMILIES):
                stable.append(execution_text(tools))

        trailing = [
            texts.WORKSPACE_GUIDANCE if tools else "",
            platform_hint(ctx.platform, configured_hints(scope)),
        ]

        context: list[str] = []
        if ctx.system_message is not None:
            context.append(ctx.system_message)
        cwd = ctx.working_dir()
        if ctx.context_files:
            context.append(
                await load_project_context(cwd, reads=reads, cap=cap, install=self.install)
            )
        if ctx.workspace_snapshot.strip():
            context.extend([ctx.workspace_snapshot, *trailing])
        else:
            stable.extend(trailing)

        runtime = ctx.runtime or local_runtime(cwd, scope.env)
        # P09-04: the frozen memory block (notes, then user profile) comes before the skills index.
        volatile = [
            ctx.memory_snapshot,
            ctx.profile_snapshot,
            ctx.memory_provider_block,
            ctx.plugin_sections,
            ctx.skills_index,
            _active_profile_line(scope),
            date_block(ctx),
            runtime_block(runtime),
        ]
        return (
            join_parts(stable),
            join_parts(context),
            join_parts(volatile),
            "persona" if persona else "",
        )
