"""Classic CLI handlers for the learning commands (P09-17, P09-20, P09-23, P09-24).

- ``/memory`` (or ``pending``) lists memory writes waiting for approval; ``approve <id…|all>`` and
  ``reject <id…|all>`` act on them; ``approval`` shows the gate, ``approval on|off`` sets it;
  ``status`` shows the stores;
- ``/skills`` (or ``list``) lists skills; ``view <name>`` prints one; ``reload`` is
  ``/reload-skills``; ``pending`` / ``approve`` / ``reject`` handle staged skill writes;
- ``/reload-skills`` rescans the skill folders and rebuilds the REPL's skill and bundle commands
  (``ctx.extensions.skill_commands``, built by ``slash.skill_cmds.skill_commands``, the one source
  the REPL also uses at start). The system prompt is not rebuilt: the agent learns about the change
  from the skills-index note on the next message (P09-07);
- ``/bundles`` lists the skill bundles;
- ``/learn <what>`` sends the prompt ``indus.learning.learn.build_learn_prompt`` builds as the next
  turn; ``/refine [focus]`` asks the session port to run the background review now (a host that
  does not offer ``refine`` says so instead of failing);
- ``/curator status|run|pause|resume`` (``run`` takes ``--dry-run``; it runs in the background so
  the chat stays usable) and ``/journey [list|delete <id>|edit <id>]``. ``delete`` here asks no
  question: typing the id is the confirmation. ``edit`` needs the editor, so it points at
  ``indus journey edit`` instead of taking over the terminal mid-chat.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from indus.cli.commands._support import scope_with_config
from indus.cli.commands.curator import run_lines, set_paused_lines
from indus.cli.commands.curator import status_lines as curator_status_lines
from indus.cli.commands.journey import delete_lines, list_lines, timeline_lines
from indus.cli.commands.memory import (
    approve_memory,
    reject_ids,
    set_approval,
    status_lines,
    which_of,
)
from indus.cli.commands.skills import approve_skills, skill_rows
from indus.cli.commands.skills_hub import slash_install, slash_search_lines
from indus.cli.slash.context import Handler, SlashContext, SlashResult
from indus.cli.slash.skill_cmds import skill_commands
from indus.learning import pending
from indus.learning.journey import nodes
from indus.learning.learn import build_learn_prompt
from indus.learning.skills.bundles import reload_bundles, scan_bundles
from indus.learning.skills.resolver import SkillError, clear_scan_cache, load_catalog
from indus.learning.skills.view_tools import clear_list_cache

__all__ = [
    "HANDLERS",
    "cmd_learn",
    "cmd_refine",
    "curator_cmd",
    "journey_cmd",
    "reload_skills",
]

_MEMORY_USAGE = (
    "Usage: /memory [pending | approve <id…|all> | reject <id…|all> | approval [on|off]]"
)
_SKILLS_USAGE = (
    "Usage: /skills [list | view <name> | reload | pending | approve <id…|all> | reject <id…|all>"
    " | search <query> | install <identifier>]"
)
_LIST_WIDTH = 60
CURATOR_USAGE = "Usage: /curator [status|run [--dry-run]|pause|resume]"
JOURNEY_USAGE = "Usage: /journey [list|delete <id>|edit <id>]"


# -- /reload-skills -------------------------------------------------------------------------------


def reload_skills(ctx: SlashContext) -> tuple[set[str], set[str]]:
    """Rescan and swap in a fresh command table; ``(added, removed)`` command names."""
    before = set(ctx.extensions.skill_commands)
    clear_scan_cache()
    clear_list_cache(ctx.scope)
    reload_bundles()
    fresh = skill_commands(ctx.scope, ctx.session_id)
    ctx.extensions.skill_commands = fresh
    after = set(fresh)
    return after - before, before - after


def _reload(ctx: SlashContext) -> SlashResult:
    added, removed = reload_skills(ctx)
    count = len(ctx.extensions.skill_commands)
    ctx.say(f"Skills reloaded: {count} skill and bundle command{'s' if count != 1 else ''}.")
    if added:
        ctx.say("New: " + ", ".join(f"/{n}" for n in sorted(added)))
    if removed:
        ctx.say("Gone: " + ", ".join(f"/{n}" for n in sorted(removed)))
    ctx.say(
        "The system prompt is unchanged; the agent is told about new skills with your next message."
    )
    return SlashResult()


# -- /memory --------------------------------------------------------------------------------------


def _approvals(ctx: SlashContext, results: list[pending.Approval]) -> None:
    if not results:
        ctx.say("Nothing is waiting for approval.")
        return
    for approval in results:
        ctx.say(pending.render_approval(approval, surface="cli"))


def memory_cmd(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    sub = words[0].lower() if words else "pending"
    rest = words[1:]
    store = pending.PendingStore(ctx.scope.home, ctx.scope.clock)
    if sub == "pending":
        ctx.say(pending.describe(store.list("memory")))
    elif sub == "status":
        ctx.say(*status_lines(ctx.scope))
    elif sub in ("approve", "reject") and not rest:
        ctx.say(f"Name the writes to {sub}: /memory {sub} <id…|all> (/memory pending lists them).")
    elif sub == "approve":
        _approvals(ctx, approve_memory(ctx.scope, which_of(rest)))
    elif sub == "reject":
        removed = reject_ids(ctx.scope, "memory", which_of(rest))
        ctx.say(f"Rejected {', '.join(removed)}." if removed else "No waiting write matched.")
    elif sub == "approval":
        _approval(ctx, rest)
    else:
        ctx.say(_MEMORY_USAGE)
    return SlashResult()


def _approval(ctx: SlashContext, rest: list[str]) -> None:
    if not rest:
        on = pending.gate_on(scope_with_config(ctx.scope), "memory")
        ctx.say(f"Memory write approval is {'on' if on else 'off'}. /memory approval on|off")
        return
    word = rest[0].lower()
    if word not in ("on", "off"):
        ctx.say(_MEMORY_USAGE)
        return
    set_approval(ctx.scope, "memory", word == "on")
    ctx.say(f"Memory write approval is now {word} (saved to the config).")


# -- /skills --------------------------------------------------------------------------------------


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _LIST_WIDTH else flat[: _LIST_WIDTH - 1] + "…"


def skills_cmd(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    sub = words[0].lower() if words else "list"
    rest = words[1:]
    if sub == "list":
        rows = skill_rows(ctx.scope)
        if not rows:
            ctx.say("No skills yet. The agent saves them with skill_manage, or try /learn.")
            return SlashResult()
        width = max(len(r["name"]) for r in rows)
        for row in rows:
            ctx.say(f"  {row['name']:<{width}}  {_clip(row['description'])}")
        ctx.say(f"{len(rows)} skills. /skills view <name> shows one; /<name> runs it.")
    elif sub == "view" and rest:
        _view(ctx, " ".join(rest))
    elif sub == "reload":
        return _reload(ctx)
    elif sub == "pending":
        store = pending.PendingStore(ctx.scope.home, ctx.scope.clock)
        ctx.say(pending.describe(store.list("skills")))
    elif sub == "approve" and rest:
        _approvals(ctx, approve_skills(ctx.scope, which_of(rest)))
    elif sub == "reject" and rest:
        removed = reject_ids(ctx.scope, "skills", which_of(rest))
        ctx.say(f"Rejected {', '.join(removed)}." if removed else "No waiting write matched.")
    elif sub == "search" and rest:
        ctx.say(*slash_search_lines(ctx.scope, " ".join(rest)))
    elif sub == "install" and len(rest) == 1:
        result = slash_install(
            ctx.scope,
            rest[0],
            confirm_install=ctx.confirm if ctx.interactive else None,
            say=ctx.say,
        )
        ctx.say(f"Installed {result.name}." if result.ok else f"Not installed: {result.message}")
        if result.ok:
            reload_skills(ctx)
    else:
        ctx.say(_SKILLS_USAGE)
    return SlashResult()


def _view(ctx: SlashContext, name: str) -> None:
    try:
        rec = load_catalog(ctx.scope, platform="cli").resolve(name)
        text = rec.skill_md.read_text(encoding="utf-8")
    except SkillError as exc:
        ctx.say(f"{exc}. /skills lists them.")
        return
    except (OSError, UnicodeDecodeError) as exc:
        ctx.say(f"Could not read {name!r}: {exc}")
        return
    ctx.say(f"# {rec.name}  ({rec.skill_dir})", text.rstrip("\n"))


def reload_skills_cmd(ctx: SlashContext, _args: str) -> SlashResult:
    return _reload(ctx)


def bundles_cmd(ctx: SlashContext, _args: str) -> SlashResult:
    found = scan_bundles(ctx.scope.home)
    if not found:
        ctx.say("No skill bundles yet. Create one with `indus bundles create <name> --skill <s>`.")
        return SlashResult()
    width = max(len(slug) for slug in found) + 1
    for slug in sorted(found):
        bundle = found[slug]
        line = f"  /{slug:<{width}} {', '.join(bundle.skills)}"
        ctx.say(line + (f"  — {bundle.description}" if bundle.description else ""))
    return SlashResult()


# -- /learn and /refine ----------------------------------------------------------------------------


def cmd_learn(ctx: SlashContext, args: str) -> SlashResult:
    """``/learn <dir|url|text>``: an empty argument learns from this conversation."""
    return SlashResult(submit=build_learn_prompt(args))


def cmd_refine(ctx: SlashContext, args: str) -> SlashResult:
    """``/refine [focus]``: review this conversation now, in the background."""
    found = getattr(ctx.port, "refine", None)
    if not callable(found):
        ctx.say("/refine is not available in this session yet.")
        return SlashResult()
    refine = cast(Callable[[str, str | None], str], found)
    try:
        line = refine(ctx.session_id, args.strip() or None)
    except Exception as exc:
        ctx.say(f"Could not start the review: {exc}")
        return SlashResult()
    ctx.say(line)
    return SlashResult()


# -- /curator and /journey -------------------------------------------------------------------------


def curator_cmd(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    verb = words[0].lower() if words else "status"
    scope = ctx.scope
    if verb == "status":
        ctx.say(*curator_status_lines(scope))
    elif verb == "run":
        flags = {w.lower() for w in words[1:]}
        unknown = flags - {"--dry-run"}
        if unknown:
            ctx.say(CURATOR_USAGE)
            return SlashResult()
        _code, lines = run_lines(scope, dry_run="--dry-run" in flags, background=True)
        ctx.say(*lines)
    elif verb in ("pause", "resume"):
        ctx.say(*set_paused_lines(scope, verb == "pause"))
    else:
        ctx.say(CURATOR_USAGE, "Everything else: `indus curator --help` in a terminal.")
    return SlashResult()


def journey_cmd(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    verb = words[0].lower() if words else ""
    scope = ctx.scope
    if not verb:
        ctx.say(*timeline_lines(nodes(scope)))
    elif verb == "list":
        ctx.say(*list_lines(nodes(scope)))
    elif verb == "delete" and len(words) == 2:
        _ok, lines = delete_lines(scope, words[1])
        ctx.say(*lines)
    elif verb == "edit" and len(words) == 2:
        ctx.say(f"Edit it from a terminal: indus journey edit {words[1]}")
    else:
        ctx.say(JOURNEY_USAGE)
    return SlashResult()


HANDLERS: dict[str, Handler] = {
    "memory": memory_cmd,
    "skills": skills_cmd,
    "reload-skills": reload_skills_cmd,
    "bundles": bundles_cmd,
    "learn": cmd_learn,
    "refine": cmd_refine,
    "curator": curator_cmd,
    "journey": journey_cmd,
}
