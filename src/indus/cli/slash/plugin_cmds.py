"""``/plugins`` and plugin slash commands in the classic REPL (P13-16, P13-23).

- ``/plugins`` (or ``/plugins list``): one line per plugin of this profile (state, kind, source, a
  waiting update), then which plugins this chat loaded;
- ``/plugins enable|disable <name>``: changes the profile's config. Plugins are loaded when the
  session host starts, so the change takes effect the next time ``indus`` starts.

``plugin_commands(scope)`` is the ``Extensions.plugin_commands`` table: a live view of the slash
commands the running host's plugins registered (``service.plugin_host``), each run on the service
loop for the current conversation. The plugin stack is imported only when a command runs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping

from indus.cli.slash.context import ExtensionHandler, Handler, SlashContext, SlashResult
from indus.foundation.scope import Scope
from indus.service import plugin_host

__all__ = ["HANDLERS", "cmd_plugins", "plugin_commands"]

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 45.0
USAGE = "Usage: /plugins [list] | /plugins enable <name> | /plugins disable <name>"


def cmd_plugins(ctx: SlashContext, args: str) -> SlashResult:
    from indus.cli.commands.plugins import plugin_lines, toggle_plugin

    words = args.split()
    verb = words[0].lower() if words else "list"
    if verb in ("enable", "disable"):
        if len(words) != 2:
            ctx.say(USAGE)
            return SlashResult()
        ctx.say(toggle_plugin(ctx.scope, words[1], verb == "enable"))
        return SlashResult()
    if verb != "list" or len(words) > 1:
        ctx.say(USAGE)
        return SlashResult()
    lines = plugin_lines(ctx.scope)
    ctx.say(*(lines or ["No plugins found. Install one with `indus plugins install`."]))
    upkeep = plugin_host.upkeep_for(ctx.scope)
    report = upkeep.report if upkeep is not None else None
    loaded = report.loaded() if report is not None else []
    if loaded:
        ctx.say(f"Loaded in this chat: {', '.join(loaded)}")
    return SlashResult()


class _PluginCommands(Mapping[str, ExtensionHandler]):
    """The running host's plugin slash commands (looked up on every access)."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def _names(self) -> list[str]:
        upkeep = plugin_host.upkeep_for(self.scope)
        return upkeep.command_names() if upkeep is not None else []

    def __getitem__(self, key: str) -> ExtensionHandler:
        name = key.lower()
        if name not in self._names():
            raise KeyError(key)

        def run(args: str, ctx: SlashContext) -> SlashResult:
            upkeep = plugin_host.upkeep_for(self.scope)
            if upkeep is None:
                ctx.say(f"/{name} is not available: no plugin host runs in this chat.")
                return SlashResult()
            cwd = ctx.session_cwd() if ctx.session_cwd is not None else None
            text = upkeep.call(
                upkeep.run_command(name, args, session_id=ctx.session_id, cwd=cwd or ""),
                timeout=COMMAND_TIMEOUT_S,
            )
            if text:
                ctx.say(text)
            return SlashResult()

        return run

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())


def plugin_commands(scope: Scope) -> Mapping[str, ExtensionHandler]:
    """``Extensions.plugin_commands`` for a chat on ``scope``."""
    return _PluginCommands(scope)


HANDLERS: dict[str, Handler] = {"plugins": cmd_plugins}
