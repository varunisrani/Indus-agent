"""Control and info commands of the classic CLI (P06-10).

``/model`` and ``/reasoning`` change the session's route through its ``RouteBinding``, all or
nothing (a refused or half-applied switch leaves the old route in place). Both are remembered on
the session row (``model_override`` = ``{provider, model}``, ``reasoning_effort``) so a resume
starts where the session left off. With ``--global`` the
choice is also saved through the config writer; leaving a custom endpoint clears the saved base
URL and wire mode so the new key is never posted to the old host (R04.9).

``/yolo`` flips the session's auto-approve switch and stores it on the session row (so a resume
keeps it). ``/approvals`` saves ``approvals.mode`` for the profile and reads it back; the approval
pipeline reads the setting on every check, so nothing has to be rebuilt. ``/tools enable|disable``
starts a fresh session with the changed toolsets, because the system prompt depends on the tools.

Every setting write is reported, never raised: a managed install or an unreadable config file
prints its message and the REPL goes on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from indus import __version__
from indus.cli import quick_commands
from indus.cli.resume import MODEL_OVERRIDE_KEY
from indus.cli.slash import session_cmds
from indus.cli.slash.context import Handler, SlashContext, SlashResult
from indus.core.cache_plan import cache_hit_percent
from indus.foundation import yamlio
from indus.foundation.config import loader, writer
from indus.foundation.config.errors import ConfigError
from indus.foundation.errors import IndusError
from indus.protocol.methods import ProtocolError
from indus.providers import route as routes
from indus.providers.pricing import format_cost
from indus.providers.registry import Registry
from indus.providers.types import EFFORT_LADDER, normalize_effort
from indus.safety.approvals import APPROVALS, session_key
from indus.safety.approvals.decision import approval_mode
from indus.safety.redaction import redact_output
from indus.service.commands.registry import CATEGORY_TITLES, REGISTRY, CommandDef
from indus.state import sessions, usage
from indus.tools import toolsets as toolset_table

__all__ = ["HANDLERS", "help_lines"]

log = logging.getLogger(__name__)

APPROVAL_MODES = ("manual", "smart", "off")
REASONING_LEVELS = ("none", *EFFORT_LADDER)
DISPLAY_WORDS = {"show": True, "hide": False}


def _flags(args: str, *names: str) -> tuple[list[str], set[str]]:
    """``(other words, flags present)`` for the boolean ``names`` (case-insensitive)."""
    words: list[str] = []
    found: set[str] = set()
    for word in args.split():
        if word.lower() in names:
            found.add(word.lower())
        else:
            words.append(word)
    return words, found


def _save_config(ctx: SlashContext, patch: dict[str, Any], what: str) -> bool:
    try:
        writer.save(ctx.scope, patch)
    except ConfigError as exc:
        hint = f" ({exc.hint})" if exc.hint else ""
        ctx.say(f"{what} was not saved: {exc.message}{hint}")
        return False
    except OSError as exc:
        ctx.say(f"{what} was not saved: {exc}")
        return False
    return True


def _fresh(ctx: SlashContext, key: str, default: Any = None) -> Any:
    try:
        view = loader.load(ctx.scope)
    except Exception:
        log.warning("config could not be read", exc_info=True)
        return default
    return view.get(key, default)


# -- /model ---------------------------------------------------------------------------------------


def _split_model(word: str, registry: Registry) -> tuple[str | None, str]:
    """``provider:model`` when the part before the colon names a provider, else the model."""
    if ":" in word:
        head, _, tail = word.partition(":")
        if tail and registry.lookup(head) is not None:
            return head, tail
    return None, word


def cmd_model(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    provider: str | None = None
    keep_global = False
    rest: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--provider" and index + 1 < len(words):
            provider = words[index + 1]
            index += 2
            continue
        if word.startswith("--provider="):
            provider = word.partition("=")[2]
        elif word == "--global":
            keep_global = True
        elif word == "--session":
            pass
        else:
            rest.append(word)
        index += 1
    before = ctx.port.route(ctx.session_id)
    if not rest and provider is None:
        ctx.say(
            f"Model: {before.model} (provider {before.provider}, "
            f"context window {before.context_length:,} tokens)",
            "Switch with /model NAME, /model PROVIDER:NAME or --provider NAME; "
            "--global also saves it.",
        )
        return SlashResult()
    if len(rest) > 1:
        ctx.say("Usage: /model [provider:model] [--provider name] [--global]")
        return SlashResult()
    registry = Registry(ctx.scope)
    named_provider, model = _split_model(rest[0], registry) if rest else (None, before.model)
    provider = provider or named_provider or before.provider
    try:
        target = routes.resolve(ctx.scope, model=model, provider=provider, registry=registry)
    except IndusError as exc:
        hint = f" ({exc.hint})" if exc.hint else ""
        ctx.say(f"Cannot switch to {model}: {exc.message}{hint}")
        return SlashResult()
    try:
        after = ctx.port.switch_route(ctx.session_id, target, keep_effort=True)
    except Exception as exc:
        message = exc.message if isinstance(exc, IndusError) else str(exc)
        log.info("model switch refused: %s", exc)
        ctx.say(
            f"Could not switch to {target.model} ({target.provider}): {message}",
            f"Still on {before.model} ({before.provider}).",
        )
        return SlashResult()
    session_cmds.remember_config(
        ctx, {MODEL_OVERRIDE_KEY: {"provider": after.provider, "model": after.model}}
    )
    ctx.say(
        f"Switched to {after.model} ({after.provider}), "
        f"context window {after.context_length:,} tokens."
    )
    if keep_global and _save_global_model(ctx, after.route):
        ctx.say("Saved as the default model.")
    return SlashResult()


def _save_global_model(ctx: SlashContext, target: routes.RuntimeRoute) -> bool:
    """Save the route as the profile default; never leave a custom endpoint's URL behind."""
    model: dict[str, Any] = {"default": target.model, "provider": target.provider}
    custom = target.profile is not None and target.profile.is_custom
    if custom:
        model["base_url"] = target.base_url
        if target.api_mode:
            model["api_mode"] = target.api_mode
    else:
        saved_url = str(_fresh(ctx, "model.base_url") or "")
        saved_mode = str(_fresh(ctx, "model.api_mode") or "")
        if saved_url:
            model["base_url"] = ""
        if saved_mode:
            model["api_mode"] = ""
    return _save_config(ctx, {"model": model}, "The model")


# -- /reasoning -----------------------------------------------------------------------------------


def cmd_reasoning(ctx: SlashContext, args: str) -> SlashResult:
    words, flags = _flags(args, "--global", "--session")
    if not words:
        info = ctx.port.route(ctx.session_id)
        level = info.reasoning_effort or "off"
        shown = "shown" if ctx.state.show_reasoning else "hidden"
        ctx.say(
            f"Reasoning effort: {level}; reasoning text is {shown}.",
            "Set with /reasoning " + "|".join(REASONING_LEVELS) + " [--global], or show/hide.",
        )
        return SlashResult()
    word = words[0].lower()
    if word in DISPLAY_WORDS and len(words) == 1:
        ctx.state.show_reasoning = DISPLAY_WORDS[word]
        ctx.say("Reasoning text will be " + ("shown." if ctx.state.show_reasoning else "hidden."))
        return SlashResult()
    if word not in REASONING_LEVELS and word != "off":
        ctx.say(
            f"Unknown reasoning setting '{words[0]}'.", "Levels: " + ", ".join(REASONING_LEVELS)
        )
        return SlashResult()
    effort = normalize_effort(word)
    info = ctx.port.route(ctx.session_id)
    try:
        after = ctx.port.switch_route(ctx.session_id, info.route, effort=effort, keep_effort=False)
    except Exception as exc:
        message = exc.message if isinstance(exc, IndusError) else str(exc)
        ctx.say(f"Reasoning effort was not changed: {message}")
        return SlashResult()
    session_cmds.remember_config(ctx, {"reasoning_effort": effort or "none"})
    applied = after.reasoning_effort or "off"
    note = "" if applied == (effort or "off") else f" (this model uses {applied})"
    scope_note = "this session"
    if "--global" in flags and _save_config(
        ctx, {"agent": {"reasoning_effort": effort or "none"}}, "The reasoning effort"
    ):
        scope_note = "this session and saved as the default"
    ctx.say(f"Reasoning effort {effort or 'off'} for {scope_note}{note}.")
    return SlashResult()


# -- /yolo, /approvals ----------------------------------------------------------------------------


def cmd_yolo(ctx: SlashContext, args: str) -> SlashResult:
    key = session_key(ctx.scope, ctx.session_id)
    enabled = not APPROVALS.auto_approve(key)

    def persist(value: bool) -> object:
        return session_cmds.remember_config(ctx, {sessions.YOLO_KEY: bool(value)})

    APPROVALS.set_auto_approve(key, enabled, persist=persist)
    if enabled:
        ctx.say(
            "Auto-approve is ON for this session: risky commands run without asking.",
            "Commands that are never allowed are still blocked. /yolo again turns it off.",
        )
    else:
        ctx.say("Auto-approve is off: risky commands ask first again.")
    return SlashResult()


def cmd_approvals(ctx: SlashContext, args: str) -> SlashResult:
    current = approval_mode(ctx.scope)
    wanted = args.strip().lower()
    if not wanted:
        ctx.say(f"Approval mode: {current} (saved for this profile).")
        return SlashResult()
    if wanted not in APPROVAL_MODES:
        ctx.say("Usage: /approvals [manual|smart|off]")
        return SlashResult()
    if not _save_config(ctx, {"approvals": {"mode": wanted}}, "The approval mode"):
        return SlashResult()
    loader.invalidate(loader.config_path(ctx.scope))
    effective = approval_mode(ctx.scope)
    if effective != wanted:
        ctx.say(f"Approval mode is still {effective}; the saved value did not take effect.")
        return SlashResult()
    ctx.say(f"Approval mode: {effective} (saved for this profile).")
    return SlashResult()


# -- /tools ---------------------------------------------------------------------------------------


def _named_sets_of(tools: Iterable[str]) -> list[str]:
    """The plain named toolsets whose tools the session has (an "everything" selection, spelled
    out); a set wholly inside another chosen one is left out, so disabling the larger one takes
    its tools away."""
    have = set(tools)
    found: dict[str, frozenset[str]] = {}
    for name in toolset_table.toolset_names():
        entry = toolset_table.TOOLSETS.get(name)
        if name.startswith(toolset_table.BUNDLE_PREFIX) or entry is None:
            continue
        if entry.includes or entry.posture or not entry.tools:
            continue
        members = frozenset(entry.tools)
        if members <= have:
            found[name] = members
    return [
        name
        for name, members in found.items()
        if not any(members < other for other in found.values())
    ]


def cmd_tools(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    action = words[0].lower() if words else "list"
    names = list(words[1:])
    tools = sorted(ctx.port.tool_names(ctx.session_id))
    if action == "list":
        ctx.say(f"Tools in this session ({len(tools)}): " + (", ".join(tools) or "none"))
        current = ctx.state.toolsets
        ctx.say("Toolsets: " + (", ".join(current) if current is not None else "the default set"))
        return SlashResult()
    if action not in ("enable", "disable") or not names:
        ctx.say("Usage: /tools [list|enable|disable] [toolset...]")
        return SlashResult()
    unknown = [n for n in names if not toolset_table.is_known(n)]
    if unknown:
        ctx.say("Unknown toolset: " + ", ".join(unknown) + ".")
        ctx.say("Known: " + ", ".join(toolset_table.toolset_names()))
        return SlashResult()
    selection = ctx.state.toolsets
    if selection is None:
        selection = _named_sets_of(tools)
    if action == "enable":
        chosen = [*selection, *(n for n in names if n not in selection)]
    else:
        chosen = [n for n in selection if n not in names]
    if chosen == selection and ctx.state.toolsets is not None:
        ctx.say("Nothing to change.")
        return SlashResult()
    ctx.say("The tools change the system prompt, so a fresh session starts now.")
    return session_cmds.start_new_session(ctx, toolsets=chosen)


# -- info -----------------------------------------------------------------------------------------


def _shown_settings(view: loader.ConfigView) -> str:
    """The effective settings as YAML for display only (never written anywhere)."""
    return yamlio.dump_plain(view.as_dict())


def cmd_config(ctx: SlashContext, args: str) -> SlashResult:
    try:
        view = loader.load(ctx.scope)
    except Exception as exc:
        ctx.say(f"The config could not be read: {exc}")
        return SlashResult()
    text = _shown_settings(view)
    ctx.say(f"Effective settings ({loader.config_path(ctx.scope)}):")
    ctx.say(redact_output(text, force=True, scope=ctx.scope).rstrip())
    return SlashResult()


def _status_lines(ctx: SlashContext) -> list[str]:
    info = ctx.port.route(ctx.session_id)
    row = sessions.get(ctx.store, ctx.session_id) if ctx.store is not None else None
    title = (row or {}).get("title") or ctx.state.pending_title or "untitled"
    count = int((row or {}).get("message_count") or 0)
    auto = APPROVALS.auto_approve(session_key(ctx.scope, ctx.session_id))
    return [
        f"Session: {ctx.session_id} ({title}), {count} messages"
        + (", reply running" if ctx.is_busy() else ""),
        f"Model: {info.model} (provider {info.provider}), "
        f"reasoning {info.reasoning_effort or 'off'}",
        f"Approvals: {approval_mode(ctx.scope)}" + (", auto-approve ON" if auto else ""),
        f"Profile: {ctx.scope.profile_id} at {ctx.scope.home}",
    ]


def cmd_status(ctx: SlashContext, args: str) -> SlashResult:
    ctx.say(*_status_lines(ctx))
    if ctx.state.queue:
        ctx.say(f"Queued messages: {len(ctx.state.queue)}")
    return SlashResult()


def cmd_context(ctx: SlashContext, args: str) -> SlashResult:
    info = ctx.port.route(ctx.session_id)
    used = 0
    if ctx.store is not None:
        totals = usage.totals(ctx.store, ctx.session_id)
        used = totals.input_tokens
    ctx.say(f"Context window: {info.context_length:,} tokens ({info.model}).")
    if used:
        ctx.say(
            f"Input tokens sent so far in this session: {used:,}. A per-request gauge arrives "
            "with P08."
        )
    else:
        ctx.say("No requests yet in this session.")
    return SlashResult()


def cmd_usage(ctx: SlashContext, args: str) -> SlashResult:
    if ctx.store is None:
        ctx.say("Usage is not recorded in this run.")
        return SlashResult()
    totals = usage.totals(ctx.store, ctx.session_id)
    ctx.say(
        f"Requests: {totals.api_calls}",
        f"Tokens in: {totals.input_tokens:,}, out: {totals.output_tokens:,}, "
        f"cache read: {totals.cache_read_tokens:,}, cache write: {totals.cache_write_tokens:,}, "
        f"reasoning: {totals.reasoning_tokens:,}",
    )
    prompt = totals.input_tokens + totals.cache_read_tokens + totals.cache_write_tokens
    hit = cache_hit_percent(prompt, totals.cache_read_tokens)
    if hit is not None and totals.cache_read_tokens:
        ctx.say(f"Prompt cache: {hit:g}% of prompt tokens read from the cache")
    try:
        status = str((sessions.get(ctx.store, ctx.session_id) or {}).get("cost_status") or "")
    except Exception:
        status = ""
    ctx.say(_cost_line(totals, status))
    return SlashResult()


def _cost_line(totals: usage.UsageTotals, status: str) -> str:
    """Cost as the store knows it (P08-08): a session with requests but no price is
    ``unknown``, never ``$0.00``; ``status`` is the newest call's cost status."""
    if totals.api_calls == 0:
        return "Cost: $0.00 (no requests yet)"
    amount = format_cost(Decimal(str(totals.cost_usd)))
    if status == "included":
        return "Cost: included (local server)"
    if totals.actual_cost_usd is not None and status == "actual":
        return f"Cost: {amount} (provider-reported)"
    if status == "estimated" and totals.cost_usd > 0:
        return f"Cost: {amount} (estimated from list prices)"
    if totals.cost_usd > 0:
        return f"Cost: {amount}+ (some requests have no known price)"
    return "Cost: unknown (no list price for this route)"


def cmd_whoami(ctx: SlashContext, args: str) -> SlashResult:
    ctx.say(
        f"Profile: {ctx.scope.profile_id}",
        f"Home: {ctx.scope.home}",
        f"Session: {ctx.session_id}",
    )
    return SlashResult()


def cmd_version(ctx: SlashContext, args: str) -> SlashResult:
    ctx.say(f"Indus Agent {__version__}")
    return SlashResult()


def help_lines(ctx: SlashContext, needle: str = "") -> list[str]:
    """``/help`` text: runnable commands grouped by category, then quick commands."""
    from indus.cli.slash.dispatch import HANDLERS

    wanted = needle.strip().lstrip("/").lower()

    def matches(cmd: CommandDef) -> bool:
        if not wanted:
            return True
        text = " ".join((*cmd.keys, REGISTRY.describe(cmd, ctx.catalogue))).lower()
        return wanted in text

    runnable = [c for c in REGISTRY.commands("cli") if c.name in HANDLERS and matches(c)]
    lines: list[str] = []
    for category, members in REGISTRY.by_category(runnable).items():
        lines.append(f"{CATEGORY_TITLES[category]}:")
        for cmd in members:
            aliases = ", ".join(f"/{a}" for a in cmd.aliases)
            alias_note = f" (also {aliases})" if aliases else ""
            lines.append(f"  {cmd.usage():<34} {REGISTRY.describe(cmd, ctx.catalogue)}{alias_note}")
    quick = [n for n in quick_commands.names(ctx.scope) if not wanted or wanted in n]
    if quick:
        lines.append("Quick commands:")
        lines.extend(f"  /{name}" for name in quick)
    if not lines:
        lines.append(f"No command matches '{needle.strip()}'.")
    else:
        lines.append("Also: !COMMAND runs a shell command here without asking the model.")
    return lines


def cmd_help(ctx: SlashContext, args: str) -> SlashResult:
    ctx.say(*help_lines(ctx, args))
    return SlashResult()


# -- running work ---------------------------------------------------------------------------------


def cmd_stop(ctx: SlashContext, args: str) -> SlashResult:
    stopped_turn = ctx.port.interrupt(ctx.session_id)
    jobs = ctx.port.stop_processes(ctx.session_id, all_jobs=True)
    parts: list[str] = []
    if stopped_turn:
        parts.append("stopped the reply")
    if jobs:
        parts.append(f"stopped {jobs} background process" + ("es" if jobs != 1 else ""))
    ctx.say(("Done: " + " and ".join(parts) + ".") if parts else "Nothing was running.")
    return SlashResult()


def cmd_steer(ctx: SlashContext, args: str) -> SlashResult:
    text = args.strip()
    if not text:
        ctx.say("Usage: /steer TEXT")
        return SlashResult()
    if not ctx.is_busy() or not ctx.port.steer(ctx.session_id, text):
        ctx.say("Nothing is running to steer; send it as a normal message instead.")
        return SlashResult()
    ctx.say("Guidance noted; the agent sees it after its next tool call.")
    return SlashResult()


def cmd_queue(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    queue = ctx.state.queue
    action = words[0].lower() if words else "list"
    if action == "list" and len(words) <= 1:
        if not queue:
            ctx.say("No queued messages.")
        else:
            ctx.say("Queued messages:")
            ctx.say(*(f"  {n}. {text}" for n, text in enumerate(queue, start=1)))
        return SlashResult()
    if action == "clear" and len(words) == 1:
        dropped = len(queue)
        queue.clear()
        ctx.say(f"Cleared {dropped} queued message" + ("s." if dropped != 1 else "."))
        return SlashResult()
    if action == "rm" and len(words) == 2 and words[1].isdigit():
        index = int(words[1])
        if not 1 <= index <= len(queue):
            ctx.say(f"There is no queued message number {index}.")
            return SlashResult()
        removed = queue.pop(index - 1)
        ctx.say(f"Removed: {removed}")
        return SlashResult()
    text = args.strip()
    if action == "add":
        text = text[len(words[0]) :].strip()
        if not text:
            ctx.say("Usage: /queue add TEXT")
            return SlashResult()
    if not ctx.is_busy():
        return SlashResult(submit=text)
    queue.append(text)
    ctx.say(f"Queued ({len(queue)} waiting); it is sent when the current reply ends.")
    return SlashResult()


def compress_args(args: str) -> tuple[str | None, int | None, bool] | None:
    """``[focus topic] [here N] [--preview]`` → ``(focus, keep_last, preview)``; None when
    ``here`` is not followed by a positive whole number."""
    words, flags = _flags(args, "--preview")
    keep_last: int | None = None
    rest: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word.lower() == "here":
            count = words[index + 1] if index + 1 < len(words) else ""
            if not count.isdigit() or int(count) < 1:
                return None
            keep_last = int(count)
            index += 2
            continue
        rest.append(word)
        index += 1
    return (" ".join(rest) or None, keep_last, "--preview" in flags)


def cmd_compress(ctx: SlashContext, args: str) -> SlashResult:
    """``/compress [focus topic] [here N] [--preview]`` (P08-05). The engine's own notice tells
    how a compaction went; this prints only what no notice covers."""
    parsed = compress_args(args)
    if parsed is None:
        ctx.say("Usage: /compress [focus topic] [here N] [--preview]  (N: messages kept as is)")
        return SlashResult()
    focus, keep_last, preview = parsed  # the dispatcher already holds it while a reply runs
    try:
        report = ctx.port.compress(ctx.session_id, focus, keep_last=keep_last, preview=preview)
    except ProtocolError as exc:
        ctx.say(f"Could not compress: {exc.message}")
        return SlashResult()
    if report.status == "preview":
        ctx.say(f"Preview: {report.reason} (about {report.before:,} tokens now); nothing changed.")
    elif report.status in ("nothing", "blocked"):
        ctx.say(report.reason or "Nothing to compress.")
    elif report.status == "compressed" and report.session_id != ctx.session_id:
        ctx.say(f"The conversation continues in session {report.session_id}.")
        ctx.session_id = report.session_id
    return SlashResult()


def cmd_quit(ctx: SlashContext, args: str) -> SlashResult:
    return SlashResult(exit=True)


HANDLERS: dict[str, Handler] = {
    "model": cmd_model,
    "reasoning": cmd_reasoning,
    "yolo": cmd_yolo,
    "approvals": cmd_approvals,
    "tools": cmd_tools,
    "config": cmd_config,
    "status": cmd_status,
    "context": cmd_context,
    "usage": cmd_usage,
    "whoami": cmd_whoami,
    "help": cmd_help,
    "version": cmd_version,
    "stop": cmd_stop,
    "steer": cmd_steer,
    "queue": cmd_queue,
    "compress": cmd_compress,
    "quit": cmd_quit,
}
