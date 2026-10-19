# pyright: strict
"""What a plugin can do: ``PluginContext``, handed to its ``register(ctx)`` (P13-16).

The ``PluginHost`` keeps everything plugins registered for one profile (commands, CLI commands,
skills, providers, prompt sections; tools go into the profile's overlay of the tool registry,
hooks onto the bus) and what each plugin added, so a plugin that fails to load can be taken back
out. Host services a plugin may use — MCP, the host model, message injection and posting — come
in through ``HostPorts``; a plugin never imports host modules, and a missing port answers with a
plain "not available".

Calls run for a conversation: the host binds a ``CallSite`` around a command, hook or tool call
(``bind_call``), and ``post_message`` / ``session_cwd`` only ever see that one.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from indus.foundation.blocking import BlockingError, DeadlineMissed, run_blocking
from indus.foundation.clock import Clock
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.plugins.hooks import Callback, HookBus, Registration, finished_within, narrow_kwargs
from indus.plugins.manifest import PluginManifest
from indus.plugins.settings import plugin_entry
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.registry import (
    NO_TRAITS,
    Probe,
    ToolEntry,
    ToolNameTaken,
    ToolRegistry,
    ToolTraits,
)
from indus.tools.registry import scope_key as registry_scope_key

__all__ = [
    "COMMAND_TIMEOUT_S",
    "MCP_RESULT_CAP",
    "PROVIDER_KINDS",
    "CallSite",
    "HostPorts",
    "PluginClosed",
    "PluginContext",
    "PluginError",
    "PluginHost",
    "PluginPermissionError",
    "Provided",
    "bind_call",
    "current_call",
]

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 30.0
MCP_RESULT_CAP = 65_536
MCP_TIMEOUT_DEFAULT_S = 30.0
MCP_TIMEOUT_MIN_S = 1.0
MCP_TIMEOUT_MAX_S = 600.0
PROMPT_ANCHORS = frozenset({"after_memory"})
PROVIDER_KINDS = (
    "image",
    "video",
    "tts",
    "stt",
    "web_search",
    "browser",
    "desktop_driver",
    "context_engine",
    "terminal_backend",
    "approval_transport",
    "model_provider",
)
SINGLE_PROVIDER_KINDS = frozenset({"context_engine"})

_SECTION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EVENT_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class PluginError(IndusError):
    default_code = "plugins.error"


class PluginPermissionError(PluginError, PermissionError):
    """The operator has not allowed this (the message names the config key that would)."""

    default_code = "plugins.permission"


class PluginClosed(PluginError):
    """The plugin's load was abandoned; nothing it registers from now on is kept."""

    default_code = "plugins.closed"


# -- call site ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSite:
    """The conversation a plugin call runs for."""

    session_key: str = ""
    conversation: str = ""
    cwd: str = ""
    surface: str = ""


_CALL: contextvars.ContextVar[CallSite | None] = contextvars.ContextVar(
    "indus_plugin_call_site", default=None
)


@contextmanager
def bind_call(site: CallSite | None) -> Generator[None]:
    """Run the block (and the tasks and workers it starts) for ``site``."""
    token = _CALL.set(site)
    try:
        yield
    finally:
        _CALL.reset(token)


def current_call() -> CallSite | None:
    return _CALL.get()


# -- host ---------------------------------------------------------------------------------------

McpCall = Callable[[str, str, dict[str, Any], float], Awaitable[str]]
LlmComplete = Callable[[list[Mapping[str, Any]], Mapping[str, Any] | None], Awaitable[Any]]
Injector = Callable[[str, str | None, Mapping[str, Any] | None, str], Awaitable[bool]]
Poster = Callable[[str, str], Awaitable[Any]]


def _no_reserved(name: str) -> bool:
    return False


@dataclass
class HostPorts:
    """Host services plugins may reach. ``reserved_commands(name)`` is true for built-in slash
    commands (the service layer passes its registry's lookup)."""

    mcp_call: McpCall | None = None
    llm_complete: LlmComplete | None = None
    inject: Injector | None = None
    post: Poster | None = None
    reserved_commands: Callable[[str], bool] = _no_reserved


@dataclass
class Provided:
    """What one plugin registered, and how to take each piece back."""

    tools: list[str] = field(default_factory=list[str])
    hooks: list[str] = field(default_factory=list[str])
    commands: list[str] = field(default_factory=list[str])
    cli_commands: list[str] = field(default_factory=list[str])
    skills: list[str] = field(default_factory=list[str])
    providers: list[str] = field(default_factory=list[str])
    prompt_sections: list[str] = field(default_factory=list[str])
    events: list[str] = field(default_factory=list[str])
    undo: list[Callable[[], object]] = field(default_factory=list[Callable[[], object]])


@dataclass(frozen=True)
class CommandEntry:
    name: str
    handler: Callback
    description: str
    args_hint: str
    plugin: str


@dataclass(frozen=True)
class CliCommandEntry:
    name: str
    command: object
    help: str
    plugin: str


@dataclass(frozen=True)
class ProviderEntry:
    kind: str
    name: str
    provider: object
    plugin: str


@dataclass(frozen=True)
class PromptSectionEntry:
    id: str
    render: str | Callable[[Mapping[str, Any]], str]
    anchor: str
    plugin: str


def _command_name(name: str) -> str:
    return name.strip().lstrip("/").strip().lower().replace(" ", "-")


class PluginHost:
    """Everything plugins registered for one profile."""

    def __init__(
        self,
        scope: Scope,
        registry: ToolRegistry,
        bus: HookBus,
        *,
        ports: HostPorts | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.scope = scope
        self.registry = registry
        self.bus = bus
        self.ports = ports if ports is not None else HostPorts()
        self.clock = clock if clock is not None else bus.clock
        self.loop: asyncio.AbstractEventLoop | None = None
        self.commands: dict[str, CommandEntry] = {}
        self.cli_commands: dict[str, CliCommandEntry] = {}
        self.skills: dict[str, Path] = {}
        self.providers: dict[str, dict[str, ProviderEntry]] = {k: {} for k in PROVIDER_KINDS}
        self.prompt_sections: dict[str, PromptSectionEntry] = {}
        self.provided: dict[str, Provided] = {}

    @property
    def profile(self) -> str:
        """The registry overlay key of this profile."""
        return registry_scope_key(self.scope)

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """The loop ``ctx.run`` hands coroutines to from worker threads (default: running one)."""
        self.loop = loop if loop is not None else asyncio.get_running_loop()

    def context(self, manifest: PluginManifest) -> PluginContext:
        self.provided.setdefault(manifest.plugin_id, Provided())
        return PluginContext(self, manifest)

    def undo(self, plugin_id: str) -> None:
        """Take back everything ``plugin_id`` registered (newest first); failures are logged."""
        provided = self.provided.pop(plugin_id, None)
        if provided is not None:
            for step in reversed(provided.undo):
                try:
                    step()
                except Exception:
                    log.warning(
                        "undoing a registration of plugin %s failed", plugin_id, exc_info=True
                    )
        self.bus.drop_owner(plugin_id)

    def has_command(self, name: str) -> bool:
        return _command_name(name) in self.commands

    async def run_command(
        self, name: str, raw_args: str = "", *, site: CallSite | None = None, **session: Any
    ) -> str:
        """Run a plugin slash command and return its text (handlers must not print).

        The handler gets ``raw_args`` plus the session keywords it declares. It is waited for at
        most ``COMMAND_TIMEOUT_S``: an async handler is then cancelled, a sync one abandoned on
        its worker; either way the answer says so.
        """
        clean = _command_name(name)
        entry = self.commands.get(clean)
        if entry is None:
            return f"Unknown command: /{clean}"
        kwargs = narrow_kwargs(entry.handler, session)
        with bind_call(site if site is not None else _CALL.get()):
            try:
                value = await self._command_value(entry, raw_args, kwargs)
            except TimeoutError:
                return (
                    f"/{clean} did not finish within {COMMAND_TIMEOUT_S:g} s and was stopped "
                    f"(plugin {entry.plugin})"
                )
            except BlockingError as exc:
                return f"/{clean} could not run right now: {exc}"
            except Exception as exc:
                log.warning("plugin command /%s raised", clean, exc_info=True)
                return f"/{clean} failed: {type(exc).__name__}: {str(exc)[:500]}"
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    async def _command_value(
        self, entry: CommandEntry, raw_args: str, kwargs: dict[str, Any]
    ) -> object:
        if inspect.iscoroutinefunction(entry.handler):
            task = asyncio.ensure_future(entry.handler(raw_args, **kwargs))
            if not await finished_within(task, COMMAND_TIMEOUT_S, self.clock):
                task.cancel()
                raise TimeoutError
            return task.result()

        def call() -> object:
            value = entry.handler(raw_args, **kwargs)
            if inspect.isawaitable(value):
                awaited = cast(Awaitable[object], value)

                async def finish() -> object:
                    return await awaited

                return asyncio.run(finish())
            return value

        try:
            return await run_blocking(
                call,
                pool="tools",
                deadline=COMMAND_TIMEOUT_S,
                clock=self.clock,
                label=f"plugin command /{entry.name}",
            )
        except DeadlineMissed as exc:
            raise TimeoutError from exc

    def render_prompt_inputs(self) -> list[PromptSectionEntry]:
        """Registered prompt sections sorted by id (rendering and freezing are P13-18)."""
        return [self.prompt_sections[k] for k in sorted(self.prompt_sections)]


# -- the context --------------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretView:
    """Read-only secrets through the profile's secret store."""

    _scope: Scope

    def get(self, name: str) -> str | None:
        store = self._scope.secrets
        return store.get(name) if store is not None else None


class PluginLlm:
    """One-shot completions on the host's auxiliary model."""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def complete(
        self, messages: Sequence[Mapping[str, Any]], schema: Mapping[str, Any] | None = None
    ) -> Any:
        port = self._ctx.host.ports.llm_complete
        if port is None:
            raise PluginError("no model is available to plugins in this host")
        return await port([dict(m) for m in messages], schema)


def _mcp_envelope(raw: object) -> dict[str, Any]:
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    truncated = len(text) > MCP_RESULT_CAP
    if truncated:
        text = text[:MCP_RESULT_CAP]
    parsed: object = None
    if not truncated:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    envelope: dict[str, Any]
    if isinstance(parsed, Mapping) and "error" in parsed:
        envelope = {"ok": False, "error": cast(Mapping[str, Any], parsed)["error"]}
    elif isinstance(parsed, Mapping) and "result" in parsed:
        found = cast(Mapping[str, Any], parsed)
        envelope = {"ok": True, "result": found["result"]}
        if "structuredContent" in found:
            envelope["structuredContent"] = found["structuredContent"]
    else:
        envelope = {"ok": True, "result": parsed if parsed is not None else text}
    if truncated:
        envelope["truncated"] = True
    return envelope


def _timeout(value: object) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        seconds = float(cast(Any, value))
    except (TypeError, ValueError):
        return MCP_TIMEOUT_DEFAULT_S
    if seconds != seconds:  # NaN
        return MCP_TIMEOUT_DEFAULT_S
    return max(MCP_TIMEOUT_MIN_S, min(seconds, MCP_TIMEOUT_MAX_S))


class PluginContext:
    """The facade one plugin registers through. Our method names; see the module docstring."""

    def __init__(self, host: PluginHost, manifest: PluginManifest) -> None:
        self.host = host
        self.manifest = manifest
        self.llm = PluginLlm(self)
        self.secrets = SecretView(host.scope)
        self._closed = False

    # -- identity -------------------------------------------------------------------------------

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    @property
    def profile_name(self) -> str:
        return self.host.scope.profile_id

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Refuse every later registration (the load was abandoned)."""
        self._closed = True

    def _provided(self) -> Provided:
        return self.host.provided.setdefault(self.plugin_id, Provided())

    def _open(self, what: str) -> None:
        if self._closed:
            log.warning(
                "plugin %s registered %s after its load was abandoned; refused",
                self.plugin_id,
                what,
            )
            raise PluginClosed(f"plugin {self.plugin_id} is closed; {what} refused")

    def _entry(self) -> Mapping[str, Any]:
        return plugin_entry(self.host.scope, self.plugin_id)

    def setting(self, key: str, default: Any = None) -> Any:
        """``plugins.entries.<id>.settings.<key>``."""
        settings = self._entry().get("settings")
        if isinstance(settings, Mapping):
            return cast(Mapping[str, Any], settings).get(key, default)
        return default

    def has_capability(self, name: str) -> bool:
        """Is capability ``name`` granted to this plugin? Unknown or unreadable → False."""
        if self.manifest.source == "bundled" and name == "tools.override":
            return True
        granted = self._entry().get("granted_capabilities")
        if not isinstance(granted, list):
            return False
        for item in cast(list[object], granted):
            if item == name:
                return True
            if isinstance(item, Mapping) and cast(Mapping[str, Any], item).get("name") == name:
                return True
        return False

    def _may_override(self) -> bool:
        return self._entry().get("allow_tool_override") is True or self.has_capability(
            "tools.override"
        )

    # -- tools and hooks ------------------------------------------------------------------------

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: Mapping[str, Any],
        handler: Callable[..., Any],
        *,
        probe: Probe | None = None,
        requires_secrets: Sequence[str] = (),
        emoji: str = "",
        max_result_chars: float | None = None,
        timeout_s: float | None = None,
        traits: ToolTraits = NO_TRAITS,
        override: bool = False,
    ) -> ToolEntry | None:
        """Add a tool for this profile (it reaches new sessions). Replacing a built-in needs
        ``override=True`` and the operator's ``plugins.entries.<id>.allow_tool_override: true``;
        without ``override`` a built-in name is refused with a warning."""
        self._open(f"tool {name!r}")
        if override and not self._may_override():
            raise PluginPermissionError(
                f"plugin {self.plugin_id} may not replace the built-in tool {name!r}; set "
                f"plugins.entries.{self.plugin_id}.allow_tool_override: true to allow it"
            )
        registry = self.host.registry
        try:
            entry = registry.register(
                name,
                toolset,
                schema,
                handler,
                probe=probe,
                requires_secrets=tuple(requires_secrets),
                emoji=emoji,
                max_result_chars=max_result_chars,
                timeout_s=timeout_s,
                traits=traits,
                override=override,
                profile=self.host.profile,
            )
        except ToolNameTaken as exc:
            log.warning("plugin %s: tool %s refused: %s", self.plugin_id, name, exc)
            return None
        provided = self._provided()
        provided.tools.append(name)
        profile = self.host.profile
        provided.undo.append(lambda: registry.deregister(name, profile=profile))
        log.debug("plugin %s registered tool %s", self.plugin_id, name)
        return entry

    def register_hook(self, name: str, callback: Callback) -> Registration:
        self._open(f"hook {name!r}")
        handle = self.host.bus.register(name, callback, owner=self.plugin_id)
        provided = self._provided()
        provided.hooks.append(name)
        provided.undo.append(handle.dispose)
        return handle

    async def dispatch_tool(self, name: str, args: Mapping[str, Any] | None = None) -> str:
        """Call a registered tool for this profile; its text result (errors are JSON text)."""
        from indus.tools.dispatch import dispatch

        site = _CALL.get()
        ctx = ToolContext(
            scope=self.host.scope, session_id=(site.session_key if site is not None else None)
        )
        result = await dispatch(ToolCall(name, dict(args or {})), ctx, registry=self.host.registry)
        return result.text

    # -- commands -------------------------------------------------------------------------------

    def register_command(
        self,
        name: str,
        handler: Callback,
        *,
        description: str = "",
        args_hint: str = "",
    ) -> CommandEntry | None:
        """A slash command ``/name`` run in the host process: ``handler(raw_args, **session)``
        returns text (sync or async). Built-in names and names another plugin holds are refused."""
        self._open(f"command {name!r}")
        clean = _command_name(name)
        if not clean:
            log.warning(
                "plugin %s registered a command with an empty name; refused", self.plugin_id
            )
            return None
        if self.host.ports.reserved_commands(clean):
            log.warning(
                "plugin %s: /%s is a built-in command; the plugin's command is refused",
                self.plugin_id,
                clean,
            )
            return None
        holder = self.host.commands.get(clean)
        if holder is not None and holder.plugin != self.plugin_id:
            log.warning(
                "plugin %s: /%s already belongs to plugin %s; refused",
                self.plugin_id,
                clean,
                holder.plugin,
            )
            return None
        entry = CommandEntry(
            clean, handler, description or "Plugin command", args_hint.strip(), self.plugin_id
        )
        self.host.commands[clean] = entry
        provided = self._provided()
        provided.commands.append(clean)
        provided.undo.append(lambda: self.host.commands.pop(clean, None))
        return entry

    def register_cli_command(self, name: str, command: object, *, help: str = "") -> bool:
        """An ``indus <name>`` subcommand: a click command or group (wired by the CLI, P13-23)."""
        self._open(f"CLI command {name!r}")
        import click

        if not isinstance(command, click.Command):
            raise TypeError(
                f"plugin {self.plugin_id}: CLI command {name!r} must be a click command"
            )
        clean = name.strip().lower()
        if not clean or clean in self.host.cli_commands:
            log.warning("plugin %s: CLI command %r refused (empty or taken)", self.plugin_id, name)
            return False
        self.host.cli_commands[clean] = CliCommandEntry(clean, command, help, self.plugin_id)
        provided = self._provided()
        provided.cli_commands.append(clean)
        provided.undo.append(lambda: self.host.cli_commands.pop(clean, None))
        return True

    # -- conversation ---------------------------------------------------------------------------

    def session_cwd(self) -> str:
        """The working folder of the conversation this call runs for ("" outside one)."""
        site = _CALL.get()
        return site.cwd if site is not None else ""

    async def inject_message(
        self,
        text: str,
        *,
        session_key: str | None = None,
        origin: Mapping[str, Any] | None = None,
        role: str = "user",
    ) -> bool:
        """Queue ``text`` for a session (``session_key``) or a chat of this profile
        (``origin``); exactly one of them. True when a host accepted it."""
        if (session_key is None) == (origin is None):
            raise ValueError("inject_message needs exactly one of session_key or origin")
        port = self.host.ports.inject
        if port is None:
            log.info("plugin %s: no message injector in this host", self.plugin_id)
            return False
        return bool(await port(text, session_key, origin, role))

    async def post_message(self, text: str) -> dict[str, Any]:
        """Post ``text`` into the conversation this call runs for — never another one."""
        site = _CALL.get()
        if site is None or not site.conversation:
            return {"ok": False, "error": "no conversation is bound to this call"}
        port = self.host.ports.post
        if port is None:
            return {"ok": False, "error": "posting messages is not available here"}
        try:
            handle = await port(site.conversation, text)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "message": handle}

    # -- skills, providers, prompt sections -----------------------------------------------------

    @property
    def skill_namespace(self) -> str:
        return self.manifest.skill_namespace or self.plugin_id.replace("/", "-")

    def register_skill(self, name: str, path: str | Path) -> str | None:
        """Offer the skill folder ``path`` as ``<namespace>:<name>``; returns that name."""
        self._open(f"skill {name!r}")
        if not _SKILL_NAME.match(name):
            raise ValueError(f"skill name {name!r} must match {_SKILL_NAME.pattern}")
        folder = Path(path)
        if folder.name == "SKILL.md":
            folder = folder.parent
        if not (folder / "SKILL.md").is_file():
            raise ValueError(f"skill {name!r}: {folder} has no SKILL.md")
        qualified = f"{self.skill_namespace}:{name}"
        if qualified in self.host.skills:
            log.warning("plugin %s: skill %s registered twice; refused", self.plugin_id, qualified)
            return None
        self.host.skills[qualified] = folder
        provided = self._provided()
        provided.skills.append(qualified)
        provided.undo.append(lambda: self.host.skills.pop(qualified, None))
        return qualified

    def register_provider(self, kind: str, provider: object, *, name: str | None = None) -> bool:
        """Offer a provider of ``kind`` (one of ``PROVIDER_KINDS``); it is used when the kind's
        own setting names it. The name comes from ``name`` or ``provider.name``."""
        self._open(f"{kind} provider")
        if kind not in PROVIDER_KINDS:
            raise ValueError(f"unknown provider kind {kind!r} (known: {', '.join(PROVIDER_KINDS)})")
        label = name if name is not None else getattr(provider, "name", None)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"a {kind} provider needs a name")
        label = label.strip()
        slot = self.host.providers[kind]
        if kind in SINGLE_PROVIDER_KINDS and slot:
            log.warning(
                "plugin %s: a %s is already registered (%s); only one is allowed",
                self.plugin_id,
                kind,
                next(iter(slot)),
            )
            return False
        if label in slot:
            log.warning(
                "plugin %s: %s provider %s already exists; refused", self.plugin_id, kind, label
            )
            return False
        slot[label] = ProviderEntry(kind, label, provider, self.plugin_id)
        provided = self._provided()
        provided.providers.append(f"{kind}:{label}")
        provided.undo.append(lambda: slot.pop(label, None))
        return True

    def register_image_provider(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("image", provider, name=name)

    def register_video_provider(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("video", provider, name=name)

    def register_tts_provider(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("tts", provider, name=name)

    def register_stt_provider(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("stt", provider, name=name)

    def register_web_search_provider(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("web_search", provider, name=name)

    def register_browser_provider(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("browser", provider, name=name)

    def register_desktop_driver(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("desktop_driver", provider, name=name)

    def register_context_engine(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("context_engine", provider, name=name)

    def register_terminal_backend(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("terminal_backend", provider, name=name)

    def register_approval_transport(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("approval_transport", provider, name=name)

    def register_model_provider(self, provider: object, *, name: str | None = None) -> bool:
        return self.register_provider("model_provider", provider, name=name)

    def register_prompt_section(
        self,
        section_id: str,
        render: str | Callable[[Mapping[str, Any]], str],
        *,
        anchor: str = "after_memory",
    ) -> PromptSectionEntry:
        """A system-prompt section rendered once per new session (P13-18 renders and freezes)."""
        self._open(f"prompt section {section_id!r}")
        if not _SECTION_ID.match(section_id):
            raise ValueError(f"prompt section id {section_id!r} must match {_SECTION_ID.pattern}")
        if anchor not in PROMPT_ANCHORS:
            raise ValueError(f"prompt section anchor must be one of {sorted(PROMPT_ANCHORS)}")
        if not isinstance(render, str) and not callable(render):
            raise TypeError("a prompt section is a string or a callable returning one")
        held = self.host.prompt_sections.get(section_id)
        if held is not None:
            raise ValueError(
                f"prompt section {section_id!r} already belongs to plugin {held.plugin}"
            )
        entry = PromptSectionEntry(section_id, render, anchor, self.plugin_id)
        self.host.prompt_sections[section_id] = entry
        provided = self._provided()
        provided.prompt_sections.append(section_id)
        provided.undo.append(lambda: self.host.prompt_sections.pop(section_id, None))
        return entry

    # -- MCP ------------------------------------------------------------------------------------

    def mcp_allowlist(self) -> frozenset[str]:
        raw = self._entry().get("mcp_allowlist")
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(str(s) for s in cast(list[object], raw) if isinstance(s, str))

    async def call_mcp(
        self,
        server: str,
        tool: str,
        args: Mapping[str, Any] | None = None,
        timeout: float = MCP_TIMEOUT_DEFAULT_S,
    ) -> dict[str, Any]:
        """Call ``tool`` on MCP ``server`` through the host's client (same gates and breaker).

        Off by default: ``server`` must be listed by name (no wildcard) in
        ``plugins.entries.<id>.mcp_allowlist``. ``timeout`` is clamped to 1–600 s. Returns
        ``{ok, result, structuredContent?}`` or ``{ok: false, error}``; text over 64 KB is cut and
        ``truncated`` set.
        """
        if server not in self.mcp_allowlist():
            raise PluginPermissionError(
                f"plugin {self.plugin_id} may not call MCP server {server!r}; add it to "
                f"plugins.entries.{self.plugin_id}.mcp_allowlist to allow it"
            )
        seconds = _timeout(timeout)
        port = self.host.ports.mcp_call
        if port is None:
            return {"ok": False, "error": "MCP is not available in this host"}
        try:
            raw = await port(server, tool, dict(args or {}), seconds)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return _mcp_envelope(raw)

    # -- files and events -----------------------------------------------------------------------

    def data_dir(self) -> Path:
        """``<home>/plugin-data/<id>/``, created; never inside the plugin's install folder."""
        folder = self.host.scope.home / "plugin-data" / self.plugin_id.replace("/", "__")
        if self.manifest.path:
            install = Path(self.manifest.path).resolve()
            if folder.resolve().is_relative_to(install):
                raise PluginError(
                    f"plugin {self.plugin_id}: data folder would be inside its install folder"
                )
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def emit(self, event: str, payload: Mapping[str, Any] | None = None) -> int:
        """Publish ``<plugin id>.<event>``; ``event`` is a bare name (the namespace is ours)."""
        if not _EVENT_NAME.match(event):
            raise ValueError(
                f"plugin {self.plugin_id}: emit a bare event name (letters, digits, _ and -); "
                f"the {self.plugin_id}. namespace is added for you"
            )
        return self.host.bus.events.emit(f"{self.plugin_id}.{event}", payload)

    def listen(self, event: str, callback: Callback) -> Registration:
        """Receive a fully named event (``indus.<event>`` or ``<plugin id>.<event>``)."""
        self._open(f"listener for {event!r}")
        handle = self.host.bus.events.listen(event, callback, owner=self.plugin_id)
        provided = self._provided()
        provided.events.append(event)
        provided.undo.append(handle.dispose)
        return handle

    def run[T](self, work: Awaitable[T], timeout: float | None = None) -> T:
        """For sync plugin code on a worker thread: run ``work`` on the host loop and wait."""
        loop = self.host.loop
        if loop is None:
            raise PluginError("the host loop is not known yet")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            if inspect.iscoroutine(work):
                work.close()
            raise RuntimeError("ctx.run() was called on the host loop; await the call instead")

        async def wrapped() -> T:
            return await work

        return asyncio.run_coroutine_threadsafe(wrapped(), loop).result(timeout)
