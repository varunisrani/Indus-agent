"""Skills and skill bundles as slash commands in the classic CLI (P09-09, P09-10).

``skill_commands(scope)`` builds the ``Extensions.skill_commands`` table once per REPL: one handler
per visible skill and per bundle (a bundle wins over a same-slug skill). Dispatch already runs
built-ins, quick commands and plugin commands first, so a skill never shadows them. A handler turns
``/<skill> [/<skill> ...] [instruction]`` into one user message (``learning.skills.invoke``) and
submits it as a turn; the system prompt is never touched. Skills added during the session are
picked up by the next REPL start (``/reload-skills`` arrives with P09-24).
"""

from __future__ import annotations

import logging

from indus.cli.slash.context import ExtensionHandler, SlashContext, SlashResult
from indus.foundation.scope import Scope
from indus.learning.skills.bundles import bundle_expanders
from indus.learning.skills.invoke import CommandTable, skill_command_names
from indus.learning.skills.resolver import load_catalog

__all__ = ["PLATFORM", "command_table", "skill_commands"]

log = logging.getLogger(__name__)

PLATFORM = "cli"


def command_table(scope: Scope, session_id: str | None = None) -> CommandTable:
    """The skills and bundles a slash word can run on the CLI now."""
    catalog = load_catalog(scope, platform=PLATFORM)
    return CommandTable(
        skills=skill_command_names(catalog), bundles=bundle_expanders(scope, session_id)
    )


def _handler(word: str) -> ExtensionHandler:
    def run(args: str, ctx: SlashContext) -> SlashResult | None:
        table = command_table(ctx.scope, ctx.session_id)
        line = f"/{word} {args}".strip()
        expansion = table.run(line, scope=ctx.scope, session_id=ctx.session_id)
        if expansion is None:
            ctx.say(f"/{word}: the skill could not be loaded; see the log for why.")
            return None
        if expansion.missing:
            ctx.say(f"Not loaded: {', '.join(expansion.missing)}.")
        return SlashResult(submit=expansion.text)

    return run


def skill_commands(scope: Scope) -> dict[str, ExtensionHandler]:
    """``{command word: handler}`` for every runnable skill and bundle (empty on any failure)."""
    try:
        names = command_table(scope).names()
    except Exception:
        log.warning("skills could not be listed; no skill commands this run", exc_info=True)
        return {}
    return {name: _handler(name) for name in sorted(names)}
