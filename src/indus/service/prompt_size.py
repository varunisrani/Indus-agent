# pyright: strict
"""How large a profile's system prompt is, part by part (P18-16 follow-up, D-16.622).

``measure(scope, platform=, session=, cwd=)`` builds the prompt the way a session of ``platform``
would, without calling any model: the P04 ``PromptBuilder`` under the P09 wrappers
(``learning.host.wire_session``: frozen memory, skills index and its auto-load block), with the
tool names the platform's selection gives (``platform_toolsets.<platform>``, else the
``indus-<platform>`` bundle) over the built-in, learning and cron tools. It answers a
``PromptSize``:

- ``system_prompt``: the whole prompt; ``tiers``: the builder's stable, context and volatile parts;
- ``sections``: ``persona`` (``PERSONA.md`` as loaded), ``context_files`` (the project context of
  the working folder), ``memory`` (the notes and user-profile blocks), ``skills`` (the index and
  the auto-load block), ``tools`` (the tool schemas as compact JSON);
- ``request``: the prompt plus the tool schemas — what every model call starts with.

Every size is characters and estimated tokens (characters / 4 rounded up, the context engine's
estimate). With ``session`` (``session_facts(row)`` of a stored row) the session's source, model and
folder are used, the memory block is cut from the session's stored prompt as a resume does, and the
stored prompt's own size is reported as ``stored_prompt``.

MCP and plugin tools, plugin prompt sections and an external memory provider's block need their
hosts running; they are not started for a measurement and are named in ``warnings`` when the
profile configures them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from indus.core.compression.anchor import estimate_text
from indus.core.prompt.builder import (
    BuildContext,
    PromptBuilder,
    PromptParts,
)
from indus.core.prompt.context_files import (
    CAP_KEY,
    ReadSettings,
    collect_warnings,
    context_cap,
    install_root,
    load_project_context,
)
from indus.core.prompt.lifecycle import Builder, StoredPrompt
from indus.core.prompt.persona import load_persona
from indus.cron import tool as cron_tool
from indus.foundation.scope import Scope
from indus.learning import host as learning_host
from indus.providers.route import resolve
from indus.tools import toolsets
from indus.tools.definitions import definitions

__all__ = ["DEFAULT_PLATFORM", "PromptSize", "SessionFacts", "Size", "measure", "session_facts"]

log = logging.getLogger(__name__)

DEFAULT_PLATFORM: Final = "cli"
ESTIMATE: Final = "chars/4"
SECTIONS: Final = ("persona", "context_files", "memory", "skills", "tools")


@dataclass(frozen=True)
class Size:
    chars: int
    tokens: int

    @classmethod
    def of(cls, text: str) -> Size:
        return cls(len(text), estimate_text(text))

    def to_json(self) -> dict[str, int]:
        return {"chars": self.chars, "tokens": self.tokens}


@dataclass(frozen=True)
class SessionFacts:
    """What a stored session contributes: its id, source, model, folder and stored prompt."""

    session_id: str
    source: str = ""
    model: str = ""
    cwd: str = ""
    stored_prompt: str | None = None


def session_facts(row: Mapping[str, Any]) -> SessionFacts:
    """``SessionFacts`` of a session row (``state.sessions.get``)."""

    def text(name: str) -> str:
        value = row.get(name)
        return value if isinstance(value, str) else ""

    stored = row.get("system_prompt")
    return SessionFacts(
        session_id=text("id"),
        source=text("source"),
        model=text("model"),
        cwd=text("cwd"),
        stored_prompt=stored if isinstance(stored, str) and stored else None,
    )


@dataclass(frozen=True)
class PromptSize:
    profile: str
    platform: str
    model: str
    provider: str
    system_prompt: Size
    request: Size
    tiers: Mapping[str, Size]
    sections: Mapping[str, Size]
    tool_count: int
    session_id: str | None = None
    stored_prompt: Size | None = None
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "session_id": self.session_id,
            "platform": self.platform,
            "model": self.model,
            "provider": self.provider,
            "estimate": ESTIMATE,
            "system_prompt": self.system_prompt.to_json(),
            "request": self.request.to_json(),
            "tiers": {name: size.to_json() for name, size in self.tiers.items()},
            "sections": {name: size.to_json() for name, size in self.sections.items()},
            "tool_count": self.tool_count,
            "stored_prompt": self.stored_prompt.to_json() if self.stored_prompt else None,
            "warnings": list(self.warnings),
        }


# -- the build ------------------------------------------------------------------------------------


@dataclass
class _Recorder:
    """The innermost builder: keeps the context the wrappers filled, then builds."""

    inner: Builder
    seen: list[BuildContext] = field(default_factory=lambda: list[BuildContext]())

    async def build(self, ctx: BuildContext) -> PromptParts:
        self.seen.append(ctx)
        return await self.inner.build(ctx)


@dataclass(frozen=True)
class _StoredOnly:
    """A read-only ``PromptStore`` holding one session's stored prompt."""

    session_id: str
    text: str | None

    async def load(self, session_id: str) -> StoredPrompt:
        if session_id != self.session_id or not self.text:
            return StoredPrompt("missing")
        return StoredPrompt("present", self.text)

    async def save(self, session_id: str, text: str) -> None:
        del session_id, text  # a measurement never writes


def _setting(scope: Scope, key: str) -> object:
    config = scope.config
    if config is None:
        return None
    try:
        return config.get(key)
    except Exception:
        log.debug("config read failed for %s", key, exc_info=True)
        return None


def _selection(scope: Scope, platform: str) -> list[str]:
    saved = _setting(scope, f"platform_toolsets.{platform}")
    if isinstance(saved, list):
        names = [str(n).strip() for n in cast(list[object], saved)]
        chosen = [n for n in names if n and n != "no_mcp"]
        if chosen:
            return chosen
    return [f"{toolsets.BUNDLE_PREFIX}{platform}"]


def _route(scope: Scope, model: str) -> tuple[str, str, Any]:
    try:
        route = resolve(scope, model=model or None)
    except Exception as exc:
        log.debug("no route for the measurement (%s)", type(exc).__name__)
        return model, "", None
    return route.model, route.provider, route


def _host_warnings(scope: Scope) -> list[str]:
    found: list[str] = []
    servers = _setting(scope, "mcp_servers")
    if isinstance(servers, Mapping) and servers:
        found.append("MCP server tools are not counted (the servers are not started)")
    plugins = _setting(scope, "plugins.enabled")
    if isinstance(plugins, list) and plugins:
        found.append("plugin tools and prompt sections are not counted (plugins are not loaded)")
    provider = _setting(scope, "memory.provider")
    if isinstance(provider, str) and provider.strip():
        found.append("the external memory provider's block is not counted")
    return found


def _folder(scope: Scope, cwd: Path | None, session: SessionFacts | None) -> Path:
    if cwd is not None:
        return cwd
    if session is not None and session.cwd:
        stored = Path(session.cwd)
        if stored.is_dir():
            return stored
    return scope.home


async def measure(
    scope: Scope,
    *,
    platform: str | None = None,
    session: SessionFacts | None = None,
    cwd: Path | None = None,
) -> PromptSize:
    """The prompt-size breakdown (see the module notes)."""
    chosen_platform = (platform or (session.source if session else "") or DEFAULT_PLATFORM).strip()
    folder = _folder(scope, cwd, session)
    model, provider, route = _route(scope, session.model if session else "")

    registry = learning_host.tool_registry(scope.clock)
    cron_tool.register_tools(registry)
    selected = toolsets.select(
        _selection(scope, chosen_platform), registry=registry, scope=scope, platform=chosen_platform
    ).tools
    schemas: Sequence[Mapping[str, Any]] = definitions(
        registry, sorted(selected), route, scope=scope
    )
    tools_json = json.dumps(list(schemas), separators=(",", ":"), ensure_ascii=False, default=str)

    session_id = session.session_id if session else ""
    stored = session.stored_prompt if session else None
    recorder = _Recorder(PromptBuilder())
    learning = learning_host.wire_session(
        scope,
        selected,
        session_id=session_id,
        prompt_store=_StoredOnly(session_id, stored) if session_id else None,
        registry=registry,
        inner=recorder,
    )
    context = BuildContext(
        scope=scope,
        model=model,
        provider=provider,
        platform=chosen_platform,
        session_id=session_id,
        tool_names=frozenset(selected),
        cwd=folder,
    )
    with collect_warnings() as read_warnings:
        parts = await learning.builder.build(context)
        filled = recorder.seen[-1] if recorder.seen else context
        reads = ReadSettings.for_scope(scope)
        cap = context_cap(None, _setting(scope, CAP_KEY))
        persona = await load_persona(scope.home, reads=reads, cap=cap) or ""
        project = await load_project_context(folder, reads=reads, cap=cap, install=install_root())

    memory = "\n\n".join(
        block.strip()
        for block in (filled.memory_snapshot, filled.profile_snapshot)
        if block.strip()
    )
    sections = {
        "persona": Size.of(persona),
        "context_files": Size.of(project),
        "memory": Size.of(memory),
        "skills": Size.of(filled.skills_index),
        "tools": Size.of(tools_json),
    }
    return PromptSize(
        profile=scope.profile_id,
        platform=chosen_platform,
        model=model,
        provider=provider,
        system_prompt=Size.of(parts.text),
        request=Size.of(parts.text + tools_json),
        tiers={
            "stable": Size.of(parts.stable),
            "context": Size.of(parts.context),
            "volatile": Size.of(parts.volatile),
        },
        sections=sections,
        tool_count=len(schemas),
        session_id=session_id or None,
        stored_prompt=Size.of(stored) if stored else None,
        warnings=tuple(dict.fromkeys([*_host_warnings(scope), *parts.warnings, *read_warnings])),
    )
