"""Skills and skill bundles as slash commands in the classic CLI (P09-09, P09-10).

``skill_commands(scope, session_id)`` builds the ``Extensions.skill_commands`` table: one handler
per visible skill and per bundle (a bundle wins over a same-slug skill). Dispatch already runs
built-ins, quick commands and plugin commands first, so a skill never shadows them. A handler turns
``/<skill> [/<skill> ...] [instruction]`` into one user message (``learning.skills.invoke``) and
submits it as a turn; the system prompt is never touched. The REPL builds the table at start and
``/reload-skills`` (``slash.learning_cmds``, P09-24) builds it again, so this is the only builder.
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
        if expansion is None or not expansion.loaded:
            ctx.say(f"/{word} could not be loaded; /reload-skills rescans the skill folders.")
            return None
        if expansion.missing:
            ctx.say(f"Not loaded: {', '.join(expansion.missing)}.")
        return SlashResult(submit=expansion.text)

    return run


def skill_commands(scope: Scope, session_id: str | None = None) -> dict[str, ExtensionHandler]:
    """``{command word: handler}`` for every runnable skill and bundle (empty on any failure)."""
    try:
        names = command_table(scope, session_id).names()
    except Exception:
        log.warning("skills could not be listed; no skill commands this run", exc_info=True)
        return {}
    return {name: _handler(name) for name in sorted(names)}
