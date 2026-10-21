"""``/cron``: scheduled jobs from inside a chat (P14-19).

- ``/cron`` or ``/cron list [all]`` — the jobs, as ``indus cron list``;
- ``/cron add <schedule> <prompt>`` — the schedule is one word (``30m``) or quoted
  (``"every 30m"``); the job delivers ``local`` unless ``--deliver <target>`` is given;
- ``/cron edit <job> [--schedule S] [--prompt P] [--name N] [--deliver D] [--skill S …]``;
- ``/cron pause|resume|remove <job>`` and ``/cron run <job> [context…]`` (the run happens in the
  background; its summary is printed when it ends).

``/suggestions`` and ``/blueprint`` (P24-46) run the shared handlers of ``indus.cron.suggestions``
and ``indus.cron.blueprints``; ``/blueprint <name>`` without values sends the agent the
blueprint's seed prompt as the next turn.

The checks are the CLI's (``indus.cli.commands.cron``); everything else lives in a terminal.
"""

from __future__ import annotations

import shlex
import threading
from typing import Any

from indus.cli.commands.cron import (
    CronRefused,
    create_spec,
    cron_backend,
    edit_changes,
    find_job,
    job_lines,
    list_lines,
    run_lines,
)
from indus.cli.slash.context import SlashContext, SlashResult
from indus.cron import ports
from indus.cron import tool as checks
from indus.cron.ports import StoreUnavailable

__all__ = ["CRON_USAGE", "HANDLERS", "blueprint_cmd", "cron_cmd", "suggestions_cmd"]

CRON_USAGE = (
    'Usage: /cron [list [all] | add <schedule> <prompt> | edit <job> --prompt "…" | '
    "pause <job> | resume <job> | run <job> [context] | remove <job>]"
)
_EDIT_KEYS = {"schedule", "prompt", "name", "deliver", "skill", "workdir", "script"}


def _options(words: list[str]) -> tuple[dict[str, Any], list[str]]:
    """``--key value`` pairs (``--skill`` may repeat) and the words left over."""
    opts: dict[str, Any] = {}
    rest: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        key = word[2:].replace("-", "_") if word.startswith("--") else ""
        if key in _EDIT_KEYS and index + 1 < len(words):
            value = words[index + 1]
            if key == "skill":
                opts.setdefault("skills", []).append(value)
            else:
                opts[key] = value
            index += 2
            continue
        if key:
            raise CronRefused(f"unknown option {word}; {CRON_USAGE}")
        rest.append(word)
        index += 1
    return opts, rest


def _add(ctx: SlashContext, backend: ports.CronBackend, words: list[str]) -> None:
    opts, rest = _options(words)
    if len(rest) < 2 and not opts.get("skills"):
        ctx.say("Usage: /cron add <schedule> <prompt>  (quote a schedule of several words)")
        return
    opts["schedule"], opts["prompt"] = rest[0], " ".join(rest[1:]) or None
    job = backend.create(create_spec(ctx.scope, backend, opts))
    ctx.say(f"Scheduled job {ports.field(job, 'id')} created.", *job_lines(job, ctx.time.now()))


def _edit(ctx: SlashContext, backend: ports.CronBackend, words: list[str]) -> None:
    if not words:
        ctx.say('Usage: /cron edit <job> --prompt "…" [--schedule …] [--name …] [--deliver …]')
        return
    job = find_job(backend, words[0])
    opts, rest = _options(words[1:])
    if rest:
        raise CronRefused(f"unexpected words: {' '.join(rest)}; {CRON_USAGE}")
    updated = backend.update(
        str(ports.field(job, "id")), edit_changes(ctx.scope, backend, job, opts)
    )
    ctx.say(f"Updated {ports.field(updated, 'id')}.", *job_lines(updated, ctx.time.now()))


def _run_in_background(
    ctx: SlashContext, backend: ports.CronBackend, job: Any, context: str | None
) -> None:
    def work() -> None:
        try:
            _code, lines = run_lines(backend, job, context)
        except Exception as exc:
            lines = [f"cron: the run failed: {type(exc).__name__}: {exc}"]
        ctx.say(*lines)

    threading.Thread(target=work, name="cron run", daemon=True).start()


def cron_cmd(ctx: SlashContext, args: str) -> SlashResult:
    try:
        words = shlex.split(args)
    except ValueError:
        ctx.say("cron: unbalanced quotes", CRON_USAGE)
        return SlashResult()
    verb = words[0].lower() if words else "list"
    rest = words[1:]
    try:
        backend = cron_backend(ctx.scope)
        if verb == "list":
            show_all = bool(rest) and rest[0].lower() == "all"
            ctx.say(*list_lines(backend, ctx.scope, show_all=show_all))
        elif verb == "add":
            _add(ctx, backend, rest)
        elif verb == "edit":
            _edit(ctx, backend, rest)
        elif verb in ("pause", "resume", "remove", "run") and rest:
            job = find_job(backend, rest[0])
            job_id = str(ports.field(job, "id"))
            if verb == "pause":
                backend.pause(job_id, " ".join(rest[1:]) or None)
                ctx.say(f"Paused {job_id}.")
            elif verb == "resume":
                backend.resume(job_id)
                ctx.say(f"Resumed {job_id}.")
            elif verb == "remove":
                if not ctx.confirm(f"Remove job {job_id}?"):
                    ctx.say("Not removed.")
                else:
                    backend.remove(job_id)
                    ctx.say(f"Removed {job_id}.")
            else:
                context = " ".join(rest[1:]) or None
                problem = checks.prompt_problem(context)
                if problem:
                    raise CronRefused(problem)
                if backend.is_running(job_id):
                    raise CronRefused(f"{job_id} is already running")
                _run_in_background(ctx, backend, job, context)
                ctx.say(f"Running {job_id} in the background; the result follows when it ends.")
        else:
            ctx.say(CRON_USAGE)
    except (CronRefused, StoreUnavailable, ValueError) as exc:
        ctx.say(f"cron: {exc}")
    return SlashResult()


def suggestions_cmd(ctx: SlashContext, args: str) -> SlashResult:
    """``/suggestions [list|accept N|dismiss N|refresh]`` (P24-46)."""
    from indus.cron.suggestions import handle_suggestions

    ctx.say(handle_suggestions(ctx.scope, args, backend=lambda: cron_backend(ctx.scope)))
    return SlashResult()


def blueprint_cmd(ctx: SlashContext, args: str) -> SlashResult:
    """``/blueprint [name [slot=value …]]`` (P24-46): a name alone hands the agent the seed."""
    from indus.cron.blueprints import handle_blueprint

    reply = handle_blueprint(ctx.scope, args, backend=lambda: cron_backend(ctx.scope))
    ctx.say(reply.text)
    return SlashResult(submit=reply.seed)


HANDLERS = {"cron": cron_cmd, "suggestions": suggestions_cmd, "blueprint": blueprint_cmd}
