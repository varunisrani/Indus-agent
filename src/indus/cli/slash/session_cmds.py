"""Session commands of the classic CLI (P06-09): new, clear, history, title, resume, sessions,
retry, undo, save, branch.

The store side is P02's: titles (``state.titles``), listings (``state.listing``), rewind and
branch (``state.rewind``) and export (``state.portability``). A fresh session has no row until
its first turn is stored, so a title or a per-session setting given before that waits in
``ctx.state`` and ``flush_pending`` writes it once the row exists (the REPL calls
``dispatch.after_turn`` when a turn ends).
"""

from __future__ import annotations

import html
import json
import logging
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from indus.cli.slash.context import Handler, SlashContext, SlashResult
from indus.foundation import fsio
from indus.foundation.clock import safe_strftime
from indus.safety.approvals import APPROVALS, session_key
from indus.state import listing, messages, portability, rewind, sessions, titles
from indus.state.registry import StoreHandle

__all__ = [
    "HANDLERS",
    "flush_pending",
    "remember_config",
    "start_new_session",
]

log = logging.getLogger(__name__)

PICKER_SIZE = 10
_SKIP_CONFIRM = frozenset({"now", "-y", "--yes"})
_FORMATS = {"json": "jsonl", "md": "md", "html": "html"}


# -- helpers --------------------------------------------------------------------------------------


def _store(ctx: SlashContext) -> StoreHandle | None:
    if ctx.store is None:
        ctx.say("Sessions are not being saved in this run, so this command cannot work here.")
    return ctx.store


def _row(ctx: SlashContext, session_id: str | None = None) -> dict[str, Any] | None:
    if ctx.store is None:
        return None
    return sessions.get(ctx.store, session_id or ctx.session_id)


def _when(ctx: SlashContext, stamp: float | None) -> str:
    if stamp is None:
        return "never"
    zone = ctx.time.now().tzinfo
    return safe_strftime(datetime.fromtimestamp(stamp, tz=zone), "%Y-%m-%d %H:%M")


def _text(value: object) -> str:
    return rewind.content_text(value)


def flush_pending(ctx: SlashContext) -> None:
    """Write the title and settings that waited for the session row (no-op until it exists)."""
    state = ctx.state
    if ctx.store is None or (state.pending_title is None and not state.pending_config):
        return
    if _row(ctx) is None:
        return
    if state.pending_config:
        patch = dict(state.pending_config)
        state.pending_config.clear()
        sessions.patch_model_config(ctx.store, ctx.session_id, patch)
    if state.pending_title is not None:
        title, state.pending_title = state.pending_title, None
        try:
            titles.set_title(ctx.store, ctx.session_id, title)
        except titles.TitleTaken as exc:
            ctx.say(f"The title could not be set: {exc}.")


def remember_config(ctx: SlashContext, patch: Mapping[str, Any]) -> bool:
    """Store per-session settings on the session row now, or once it exists. True when written."""
    if ctx.store is None:
        return False
    if sessions.patch_model_config(ctx.store, ctx.session_id, patch):
        return True
    ctx.state.pending_config.update(patch)
    return False


def start_new_session(
    ctx: SlashContext,
    *,
    title: str | None = None,
    toolsets: list[str] | None = None,
    clear: bool = False,
) -> SlashResult:
    """Create a session with the chat's creation flags, close the current one and switch."""
    update: dict[str, Any] = {"title": title, "idempotency_key": None}
    if toolsets is not None:
        update["toolsets"] = list(toolsets)
    params = ctx.create_params.model_copy(update=update)
    new_id = ctx.port.create(params)
    old = ctx.session_id
    try:
        ctx.port.stop_processes(old, all_jobs=False)
    except Exception:
        log.warning("could not release the background processes of %s", old, exc_info=True)
    ctx.port.close(old)
    APPROVALS.clear_session(session_key(ctx.scope, old))
    ctx.state.forget_session()
    if toolsets is not None:
        ctx.state.toolsets = list(toolsets)
    if title:
        ctx.state.pending_title = title
    ctx.say(f"Started a new session ({new_id}).")
    return SlashResult(switch_to=new_id, clear_screen=clear)


def _has_history(ctx: SlashContext) -> bool:
    row = _row(ctx)
    if ctx.store is None:
        return True
    return row is not None and int(row.get("message_count") or 0) > 0


# -- /new, /clear ---------------------------------------------------------------------------------


def cmd_new(ctx: SlashContext, args: str) -> SlashResult:
    words = args.split()
    skip = any(word.lower() in _SKIP_CONFIRM for word in words)
    name = " ".join(word for word in words if word.lower() not in _SKIP_CONFIRM)
    if not skip and _has_history(ctx):
        question = "Start a new session? The current one stays saved and can be resumed."
        if not ctx.confirm(question):
            ctx.say("Kept the current session.")
            return SlashResult()
    return start_new_session(ctx, title=name or None)


def cmd_clear(ctx: SlashContext, args: str) -> SlashResult:
    return start_new_session(ctx, clear=True)


# -- /history -------------------------------------------------------------------------------------


def cmd_history(ctx: SlashContext, args: str) -> SlashResult:
    store = _store(ctx)
    if store is None:
        return SlashResult()
    rows = messages.get(store, ctx.session_id)
    if not rows:
        ctx.say("No messages in this session yet.")
        return SlashResult()
    for row in rows:
        role = row.get("role")
        text = _text(row.get("content")).strip()
        if role == "user":
            ctx.say(f"You: {text}")
        elif role == "assistant":
            calls = row.get("tool_calls") or []
            if text:
                ctx.say(f"Agent: {text}")
            names = [_call_name(call) for call in calls] if isinstance(calls, list) else []
            if names:
                ctx.say("  (used " + ", ".join(names) + ")")
        elif role == "tool":
            name = row.get("tool_name") or "tool"
            ctx.say(f"  [{name}: {len(text)} characters of output]")
    return SlashResult()


def _call_name(call: object) -> str:
    if isinstance(call, Mapping):
        function = call.get("function")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(function, Mapping):
            name = function.get("name")  # pyright: ignore[reportUnknownMemberType]
            if isinstance(name, str):
                return name
        name = call.get("name")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(name, str):
            return name
    return "a tool"


# -- /title ---------------------------------------------------------------------------------------


def cmd_title(ctx: SlashContext, args: str) -> SlashResult:
    store = _store(ctx)
    if store is None:
        return SlashResult()
    wanted = args.strip()
    if not wanted:
        current = titles.get_title(store, ctx.session_id) or ctx.state.pending_title
        ctx.say(f"Title: {current}" if current else "This session has no title yet.")
        return SlashResult()
    clean = titles.sanitize(wanted)
    if clean is None:
        ctx.say("That title is empty once control characters are removed.")
        return SlashResult()
    try:
        written = titles.set_title(store, ctx.session_id, clean)
    except titles.TitleTaken as exc:
        ctx.say(f"The title could not be set: {exc}.")
        return SlashResult()
    if written:
        ctx.say(f"Title set: {clean}")
    else:
        ctx.state.pending_title = clean
        ctx.say(f"Title set: {clean} (saved with the first reply).")
    return SlashResult()


# -- /resume, /sessions ---------------------------------------------------------------------------


def _recent(store: StoreHandle) -> list[listing.SessionSummary]:
    return listing.list_sessions(store, listing.ListFilters(), limit=PICKER_SIZE)


def _show_picker(ctx: SlashContext, store: StoreHandle) -> SlashResult:
    found = _recent(store)
    if not found:
        ctx.say("There are no saved sessions yet.")
        ctx.state.resume_choices = None
        return SlashResult()
    ctx.say("Recent sessions:")
    for number, item in enumerate(found, start=1):
        label = item.title or item.preview or "(no messages)"
        here = "  <- this one" if item.id == ctx.session_id else ""
        ctx.say(
            f"  {number:>2}. {label}  [{item.id}, {item.message_count} messages, "
            f"{_when(ctx, item.last_active)}]{here}"
        )
    ctx.say("Type a number to resume that session, or /resume NAME.")
    ctx.state.resume_choices = [item.id for item in found]
    return SlashResult()


def _strip_brackets(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] + value[-1] in ("<>", "[]", '""', "''"):
        value = value[1:-1].strip()
    return value


def _resolve_target(ctx: SlashContext, store: StoreHandle, target: str) -> str | None:
    if target.isdigit():
        choices = ctx.state.resume_choices or [item.id for item in _recent(store)]
        index = int(target)
        if not 1 <= index <= len(choices):
            ctx.say(f"There is no session number {index} in the list.")
            return None
        return choices[index - 1]
    by_title = titles.resolve_title(store, target)
    if by_title is not None:
        return by_title
    try:
        return sessions.resolve_id(store, target)
    except sessions.AmbiguousSessionId as exc:
        ctx.say(f"{exc}. Give more of the id.")
    except sessions.SessionNotFound:
        ctx.say(f"No session found matching '{target}'.", "Type /sessions to see recent ones.")
    return None


def cmd_resume(ctx: SlashContext, args: str) -> SlashResult:
    store = _store(ctx)
    if store is None:
        return SlashResult()
    target = _strip_brackets(args)
    if not target:
        return _show_picker(ctx, store)
    session_id = _resolve_target(ctx, store, target)
    ctx.state.resume_choices = None
    if session_id is None:
        return SlashResult()
    if session_id == ctx.session_id:
        ctx.say("You are already in that session.")
        return SlashResult()
    ctx.port.resume(session_id)
    old = ctx.session_id
    ctx.port.close(old)
    ctx.state.forget_session()
    row = sessions.get(store, session_id) or {}
    title = row.get("title")
    ctx.say(f"Resumed {session_id}" + (f" ({title})." if title else "."))
    return SlashResult(switch_to=session_id, resumed=True)


def cmd_sessions(ctx: SlashContext, args: str) -> SlashResult:
    target = args.strip()
    if target and target.lower() not in ("list", "ls"):
        return cmd_resume(ctx, target)
    store = _store(ctx)
    if store is None:
        return SlashResult()
    return _show_picker(ctx, store)


# -- /retry, /undo --------------------------------------------------------------------------------


def cmd_retry(ctx: SlashContext, args: str) -> SlashResult:
    store = _store(ctx)
    if store is None:
        return SlashResult()
    try:
        taken = rewind.rewind(store, ctx.session_id, -1, None)
    except rewind.RewindUnavailable:
        ctx.say("There is no message to send again yet.")
        return SlashResult()
    if not taken.target_text.strip():
        ctx.say("The last message had no text to send again.")
        return SlashResult()
    ctx.say("Sending your last message again.")
    return SlashResult(submit=taken.target_text)


def cmd_undo(ctx: SlashContext, args: str) -> SlashResult:
    store = _store(ctx)
    if store is None:
        return SlashResult()
    raw = args.strip() or "1"
    if not raw.isdigit() or int(raw) < 1:
        ctx.say("Usage: /undo [N]  (N is how many of your messages to take back, default 1)")
        return SlashResult()
    count = int(raw)
    have = len(rewind.recent_user_messages(store, ctx.session_id, count))
    if have == 0:
        ctx.say("There is nothing to undo yet.")
        return SlashResult()
    try:
        taken = rewind.rewind(store, ctx.session_id, -count, None)
    except rewind.RewindUnavailable:
        ctx.say("There is nothing to undo yet.")
        return SlashResult()
    noun = "message" if have == 1 else "messages"
    ctx.say(f"Took back your last {have} {noun} and the replies to them.")
    return SlashResult(prefill=taken.target_text or None)


# -- /save ----------------------------------------------------------------------------------------


def _save_usage(ctx: SlashContext) -> SlashResult:
    ctx.say("Usage: /save json|md|html [file]")
    return SlashResult()


def cmd_save(ctx: SlashContext, args: str) -> SlashResult:
    store = _store(ctx)
    if store is None:
        return SlashResult()
    words = args.split(maxsplit=1)
    if not words or words[0].lower() not in _FORMATS:
        return _save_usage(ctx)
    kind = words[0].lower()
    row = _row(ctx)
    if row is None:
        ctx.say("There is nothing to save yet.")
        return SlashResult()
    name = (
        words[1].strip() if len(words) > 1 else f"indus-session-{ctx.session_id}.{_FORMATS[kind]}"
    )
    path = Path(os.path.expanduser(name))
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists():
        ctx.say(f"{path} already exists; give another file name.")
        return SlashResult()
    if kind == "json":
        objects = list(portability.export_session(store, ctx.session_id))
        text = "".join(portability.dump_line(obj) + "\n" for obj in objects)
        count = sum(1 for obj in objects if obj.get("kind") == "message")
    else:
        rows = messages.get(store, ctx.session_id)
        count = len(rows)
        title = str(row.get("title") or f"Session {ctx.session_id}")
        render = _markdown if kind == "md" else _html
        text = render(title, ctx.session_id, rows)
    fsio.atomic_write_text(path, text)
    ctx.say(f"Saved {count} messages to {path}")
    return SlashResult()


_SPEAKERS = {"user": "You", "assistant": "Indus Agent", "system": "System"}


def _markdown(title: str, session_id: str, rows: list[dict[str, Any]]) -> str:
    parts = [f"# {title}", "", f"Session `{session_id}`", ""]
    for row in rows:
        role = str(row.get("role"))
        text = _text(row.get("content")).rstrip()
        if role == "tool":
            parts += [f"**{row.get('tool_name') or 'tool'} output**", "", "```", text, "```", ""]
            continue
        parts += [f"## {_SPEAKERS.get(role, role)}", "", text, ""]
        calls = row.get("tool_calls")
        if isinstance(calls, list) and calls:
            for call in calls:  # pyright: ignore[reportUnknownVariableType]
                parts += [f"- used `{_call_name(call)}`: `{json.dumps(_args_of(call))}`"]
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _args_of(call: object) -> object:
    if isinstance(call, Mapping):
        function = call.get("function")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(function, Mapping):
            return function.get("arguments")  # pyright: ignore[reportUnknownMemberType]
    return None


def _html(title: str, session_id: str, rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        role = str(row.get("role"))
        text = html.escape(_text(row.get("content")))
        if role == "tool":
            label = html.escape(str(row.get("tool_name") or "tool"))
            body.append(f'<section class="tool"><h3>{label} output</h3><pre>{text}</pre></section>')
            continue
        speaker = html.escape(_SPEAKERS.get(role, role))
        body.append(f'<section class="{html.escape(role)}"><h2>{speaker}</h2><pre>{text}</pre>')
        calls = row.get("tool_calls")
        if isinstance(calls, list) and calls:
            used = ", ".join(html.escape(_call_name(c)) for c in calls)  # pyright: ignore[reportUnknownVariableType]
            body.append(f"<p>Used: {used}</p>")
        body.append("</section>")
    head = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        "<style>body{font-family:sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}"
        "pre{white-space:pre-wrap;word-wrap:break-word}.tool pre{background:#f4f4f4}</style>\n"
        "</head>\n<body>\n"
        f"<h1>{html.escape(title)}</h1>\n<p>Session <code>{html.escape(session_id)}</code></p>\n"
    )
    return head + "\n".join(body) + "\n</body>\n</html>\n"


# -- /branch --------------------------------------------------------------------------------------


def cmd_branch(ctx: SlashContext, args: str) -> SlashResult:
    store = _store(ctx)
    if store is None:
        return SlashResult()
    if not _has_history(ctx):
        ctx.say("There is nothing to branch yet; send a message first.")
        return SlashResult()
    name = args.strip() or None
    try:
        child = rewind.branch(store, ctx.session_id, title=name)
    except titles.TitleTaken as exc:
        ctx.say(f"The branch could not take that title: {exc}.")
        return SlashResult()
    ctx.port.resume(child)
    old = ctx.session_id
    ctx.port.close(old)
    ctx.state.forget_session()
    title = titles.get_title(store, child) or child
    ctx.say(f"Branched into '{title}' ({child}).", f"The original session is unchanged: {old}")
    return SlashResult(switch_to=child, resumed=True)


HANDLERS: dict[str, Handler] = {
    "new": cmd_new,
    "clear": cmd_clear,
    "history": cmd_history,
    "title": cmd_title,
    "resume": cmd_resume,
    "sessions": cmd_sessions,
    "retry": cmd_retry,
    "undo": cmd_undo,
    "save": cmd_save,
    "branch": cmd_branch,
}
