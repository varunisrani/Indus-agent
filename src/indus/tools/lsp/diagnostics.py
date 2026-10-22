# pyright: strict
"""Language-server errors an edit introduced, reported without holding the edit (P24-16).

``EditDiagnostics.check_edit(path, before, after, owner=…)`` schedules one check and waits for it
at most ``WRITE_BUDGET_S`` (0.08 s, measured on the clock from when the edit tool started):

- the check syncs ``after`` (the bytes the edit wrote, never a later read of the file) and waits
  for the server's verdict (``lsp.wait_timeout``); no verdict in time means "no data", never
  "clean", and nothing is reported;
- only when that verdict holds errors (severity 1 or none given) is the verdict on ``before``
  needed: taken from the cache of earlier verdicts when it saw exactly those bytes (≤256 files),
  else synced and asked for. Its errors are moved to their lines after the edit (``line_map``;
  ones on deleted lines drop out) and removed, so moved old errors never look new;
- the server is left holding the newest content written to the file;
- one check is bounded at ``CHECK_CEILING_S`` (60 s, a cold spawn included); past that, or when
  the server dies, the (server, root) pair is marked broken.

A verdict that came within the budget is returned to the edit tool (``lint.lint_edit`` adds it to
the lint report). A later one becomes a note queued for the owner (the session, else the task):
≤``MAX_OWNERS`` (64) owners, ≤``MAX_PER_OWNER`` (32) files each, oldest dropped first. Notes ride
the owner's next tool result (``LspToolHooks.transform``) or, at the end of a turn, the LSP stop
gate (``settle`` waits up to ``TURN_END_WAIT_S`` = 5 s for the owner's running checks).

Checks of one file run one after the other. A newer edit of a file by the same owner takes the
older unfinished check over: it diffs against the older check's ``before`` and carries the older
undelivered note, so an error the first edit made is still reported unless the second fixed it;
if the newer check gets no verdict the older result (or note) is delivered instead.

Checkers are installed per profile tier with ``install_active`` (wave 45, SEC-14: one per served
profile's home, see the section notes); ``release_folder`` stops the servers of a folder about to
be removed (a subagent's worktree, wave 45) in every installed tier. Who is editing, and under
which profile, is bound per tool call (``bind_owner`` / ``bind_tier``, done by
``LspToolHooks.before``). The files a turn edited are kept per (owner, turn) for the stop gates
(``turn_edits``).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Callable, Generator, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope, scope_or_none
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.hooks import NO_HOOKS, BeforeVerdict, ToolHooks
from indus.tools.lsp.client import LspClient, as_synced, diagnostic_key, within
from indus.tools.lsp.manager import LspManager
from indus.tools.lsp.servers import language_id

__all__ = [
    "BASELINE_CAP",
    "CHECK_CEILING_S",
    "MAX_OWNERS",
    "MAX_PER_OWNER",
    "PROCESS_WIDE",
    "TURN_END_WAIT_S",
    "WRITE_BUDGET_S",
    "CheckOutcome",
    "EditDiagnostics",
    "LspToolHooks",
    "active",
    "bind_owner",
    "bind_tier",
    "current_owner",
    "format_errors",
    "install_active",
    "installed",
    "is_error",
    "line_map",
    "record_edit",
    "release_folder",
    "shift_all",
    "shift_range",
    "tier_key",
]

log = logging.getLogger(__name__)

WRITE_BUDGET_S = 0.08
BASELINE_CAP = 256
MAX_OWNERS = 64
MAX_PER_OWNER = 32
TURN_END_WAIT_S = 5.0
CHECK_CEILING_S = 60.0
MAX_LISTED = 20
MAX_TURNS_KEPT = 256
MAX_EDITS_PER_TURN = 512

type Diagnostics = list[dict[str, Any]]
type LineMap = Callable[[int], int | None]


# -- moving diagnostics across an edit ------------------------------------------------------------


def line_map(before: str, after: str) -> LineMap:
    """``map(old_line) -> new_line`` (0-based) across the edit; None for a deleted line."""
    old_lines = before.splitlines()
    new_lines = after.splitlines()
    if old_lines == new_lines:
        return lambda line: line
    blocks = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_opcodes()

    def mapped(line: int) -> int | None:
        for tag, a_start, a_end, b_start, _b_end in blocks:
            if line < a_start:
                break
            if a_start <= line < a_end:
                return b_start + (line - a_start) if tag == "equal" else None
        # past the old text's end (a diagnostic at EOF): pin it to the new end
        return max(0, len(new_lines) - 1) if new_lines else None

    return mapped


def shift_range(item: Mapping[str, Any], mapping: LineMap) -> dict[str, Any] | None:
    """A copy of ``item`` with its range in post-edit lines; None when its first line is gone.
    An end line that was deleted collapses onto the start line."""
    rng = cast(Mapping[str, Any], item.get("range") or {})
    start = cast(Mapping[str, Any], rng.get("start") or {})
    end = cast(Mapping[str, Any], rng.get("end") or {})
    start_line = int(start.get("line", 0))
    new_start = mapping(start_line)
    if new_start is None:
        return None
    new_end = mapping(int(end.get("line", start_line)))
    if new_end is None or new_end < new_start:
        new_end = new_start
    return {
        **item,
        "range": {
            "start": {"line": new_start, "character": int(start.get("character", 0))},
            "end": {"line": new_end, "character": int(end.get("character", 0))},
        },
    }


def shift_all(items: Sequence[Mapping[str, Any]], mapping: LineMap) -> Diagnostics:
    moved = (shift_range(item, mapping) for item in items)
    return [item for item in moved if item is not None]


def is_error(item: Mapping[str, Any]) -> bool:
    severity = item.get("severity")
    return severity is None or severity == 1


def format_errors(server: str, path: str, items: Sequence[Mapping[str, Any]]) -> str:
    """The text the model reads: one line per error, 1-based ``line:column``."""
    count = len(items)
    noun = "error" if count == 1 else "errors"
    lines = [f"{server} reports {count} new {noun} in {path}:"]
    for item in items[:MAX_LISTED]:
        rng = cast(Mapping[str, Any], item.get("range") or {})
        start = cast(Mapping[str, Any], rng.get("start") or {})
        where = f"{int(start.get('line', 0)) + 1}:{int(start.get('character', 0)) + 1}"
        code = item.get("code")
        tag = f" [{code}]" if code not in (None, "") else ""
        message = " ".join(str(item.get("message") or "").split())
        lines.append(f"  {where} {message}{tag}")
    if count > MAX_LISTED:
        lines.append(f"  ... and {count - MAX_LISTED} more")
    return "\n".join(lines)


def _digest(text: str) -> str:
    return hashlib.sha256(as_synced(text).encode("utf-8", "surrogatepass")).hexdigest()


# -- outcomes -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """What the edit tool learns. ``inline``: the verdict came within the budget (``errors``
    are the new ones, empty when clean; ``server`` names who said so). ``deferred``: it will
    come later as a note. ``none``: no verdict (or LSP does not apply)."""

    status: Literal["inline", "deferred", "none"]
    errors: tuple[dict[str, Any], ...] = ()
    server: str = ""

    def text(self, path: str) -> str | None:
        if self.status != "inline" or not self.errors:
            return None
        return format_errors(self.server, path, self.errors)


@dataclass
class _Note:
    text: str
    baseline: str | None


@dataclass
class _Check:
    """One scheduled check (the record a newer edit by the same owner takes over)."""

    path: str
    owner: str | None
    baseline: str | None
    carried: list[_Note] = field(default_factory=lambda: list[_Note]())
    task: asyncio.Task[None] | None = None
    superseded: bool = False
    finished: bool = False
    claim: Literal["", "inline", "deferred"] = ""
    errors: Diagnostics | None = None
    server: str = ""


class EditDiagnostics:
    """Per-process edit checks (see the module notes)."""

    def __init__(
        self,
        manager: LspManager,
        *,
        clock: Clock | None = None,
        wait_timeout: float | None = None,
        write_budget: float = WRITE_BUDGET_S,
        ceiling: float = CHECK_CEILING_S,
    ) -> None:
        self.manager = manager
        self.clock: Clock = clock or manager.clock
        self.wait_timeout = (
            wait_timeout if wait_timeout is not None else manager.settings.wait_timeout
        )
        self.write_budget = write_budget
        self.ceiling = ceiling
        self._verdicts: OrderedDict[str, tuple[str, Diagnostics]] = OrderedDict()
        self._tail: dict[str, _Check] = {}
        self._inflight: dict[tuple[str, str], _Check] = {}
        self._newest: dict[str, str] = {}
        self._notes: OrderedDict[str, OrderedDict[str, _Note]] = OrderedDict()
        self._pending: dict[str, set[asyncio.Task[None]]] = {}

    def handles(self, path: str) -> bool:
        return self.manager.handles(path)

    # -- verdict cache ----------------------------------------------------------------------

    def _remember(self, path: str, text: str, items: Diagnostics) -> None:
        self._verdicts.pop(path, None)
        self._verdicts[path] = (_digest(text), list(items))
        while len(self._verdicts) > BASELINE_CAP:
            self._verdicts.popitem(last=False)

    def _recall(self, path: str, text: str) -> Diagnostics | None:
        hit = self._verdicts.get(path)
        return list(hit[1]) if hit is not None and hit[0] == _digest(text) else None

    def cached_paths(self) -> list[str]:
        return list(self._verdicts)

    # -- notes ------------------------------------------------------------------------------

    def post(self, owner: str, path: str, text: str, baseline: str | None) -> None:
        """Queue ``text`` for ``owner``; a note already queued for ``path`` stays in front."""
        notes = self._notes.pop(owner, None) or OrderedDict[str, _Note]()
        older = notes.pop(path, None)
        if older is not None and older.text != text:
            text, baseline = f"{older.text}\n\n{text}", older.baseline
        notes[path] = _Note(text, baseline)
        while len(notes) > MAX_PER_OWNER:
            notes.popitem(last=False)
        self._notes[owner] = notes
        while len(self._notes) > MAX_OWNERS:
            self._notes.popitem(last=False)

    def take_notes(self, owner: str | None) -> list[str]:
        """Every queued note of ``owner``, oldest first; they are gone afterwards."""
        if not owner:
            return []
        notes = self._notes.pop(owner, None)
        return [note.text for note in notes.values()] if notes else []

    def _withdraw(self, owner: str, path: str) -> _Note | None:
        notes = self._notes.get(owner)
        return notes.pop(path, None) if notes else None

    def owners(self) -> list[str]:
        return list(self._notes)

    def running(self, owner: str) -> int:
        return len(self._pending.get(owner, ()))

    async def settle(self, owner: str | None, timeout: float = TURN_END_WAIT_S) -> list[str]:
        """Turn end: wait up to ``timeout`` for ``owner``'s running checks, then take its notes."""
        if not owner:
            return []
        running = [t for t in self._pending.get(owner, ()) if not t.done()]
        if running:
            with contextlib.suppress(TimeoutError):
                await within(self.clock, asyncio.wait(running), timeout)
        return self.take_notes(owner)

    # -- checks -----------------------------------------------------------------------------

    async def check_edit(
        self,
        path: str,
        before: str | None,
        after: str | None,
        *,
        owner: str | None = None,
        started: float | None = None,
    ) -> CheckOutcome:
        """Check the edit of ``path`` (``before`` None: a new file; ``after`` None: read the
        file when the check runs); see the module notes."""
        path = os.path.abspath(path)
        if not self.handles(path):
            return CheckOutcome("none")
        start = self.clock.monotonic() if started is None else started
        check = self._schedule(path, before, after, owner)
        task = check.task
        assert task is not None
        budget = max(0.0, start + self.write_budget - self.clock.monotonic())
        if budget > 0 and not task.done():
            with contextlib.suppress(TimeoutError):
                await within(self.clock, asyncio.shield(task), budget)
        if check.finished:
            check.claim = "inline"
            if check.errors is None:
                if not check.superseded:
                    self._deliver(check)  # restores the older notes it carried
                return CheckOutcome("none")
            return CheckOutcome("inline", tuple(check.errors), check.server)
        check.claim = "deferred"
        return CheckOutcome("deferred") if owner else CheckOutcome("none")

    def _schedule(
        self, path: str, before: str | None, after: str | None, owner: str | None
    ) -> _Check:
        if after is not None:
            self._newest[path] = after
        else:
            self._newest.pop(path, None)
        check = _Check(path, owner, before)
        older = self._inflight.get((owner, path)) if owner else None
        if older is not None and not older.finished:
            older.superseded = True
            check.baseline = older.baseline
            check.carried = [*older.carried]
            older.carried = []
        if owner:
            note = self._withdraw(owner, path)
            if note is not None:
                check.baseline = note.baseline
                check.carried.insert(0, note)
        previous = self._tail.get(path)
        check.task = asyncio.create_task(self._run(check, after, previous, older))
        self._tail[path] = check
        if owner:
            self._inflight[(owner, path)] = check
            pending = self._pending.setdefault(owner, set())
            pending.add(check.task)
            check.task.add_done_callback(lambda t, o=owner: self._done(o, t))
        return check

    def _done(self, owner: str, task: asyncio.Task[None]) -> None:
        tasks = self._pending.get(owner)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                del self._pending[owner]

    async def _run(
        self,
        check: _Check,
        after: str | None,
        previous: _Check | None,
        older: _Check | None,
    ) -> None:
        try:
            waits = {c.task for c in (previous, older) if c is not None and c.task is not None}
            if waits:
                await asyncio.wait(waits)
            fallback = older if older is not None and older.errors is not None else None
            if check.superseded:
                check.errors = fallback.errors if fallback is not None else None
                check.server = fallback.server if fallback is not None else ""
                return
            try:
                found = await within(
                    self.clock, self._check(check, check.baseline, after), self.ceiling
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # timeout or a dead server: no verdict, pair marked broken
                log.debug("LSP check of %s failed: %s", check.path, exc)
                await self.manager.drop(check.path, f"{type(exc).__name__}: {exc}")
                found = None
            if found is None and fallback is not None:
                check.errors, check.server = fallback.errors, fallback.server
            else:
                check.errors = found
        finally:
            self._finish(check)

    def _finish(self, check: _Check) -> None:
        check.finished = True
        if self._tail.get(check.path) is check:
            del self._tail[check.path]
        key = (check.owner or "", check.path)
        if check.owner and self._inflight.get(key) is check:
            del self._inflight[key]
        if check.owner is None or check.superseded or check.claim != "deferred":
            return  # a newer check took over, or the edit tool is still waiting for this one
        self._deliver(check)

    def _deliver(self, check: _Check) -> None:
        owner = check.owner
        if owner is None:
            return
        if check.errors is None:
            for note in check.carried:  # no verdict: the older notes still stand
                self.post(owner, check.path, note.text, note.baseline)
        elif check.errors:
            text = format_errors(check.server, check.path, check.errors)
            self.post(owner, check.path, text, check.baseline)

    async def _check(
        self, check: _Check, before: str | None, after: str | None
    ) -> Diagnostics | None:
        path = check.path
        client = await self.manager.client_for(path)
        if client is None:
            return None
        spec = self.manager.server_for(path)
        language = language_id(path, spec)
        check.server = client.server_id
        version = await client.sync(path, after, language=language)
        await client.save(path)
        verdict = await client.diagnostics(path, version, timeout=self.wait_timeout)
        self.manager.touch_client(client)
        if verdict is None:
            await self._resync(client, path, language)
            return None
        synced = client.synced_text(path) or ""
        errors = [d for d in verdict if is_error(d)]
        if errors and before is not None:
            base = self._recall(path, before)
            if base is None:
                base_version = await client.sync(path, before, language=language)
                base = await client.diagnostics(path, base_version, timeout=self.wait_timeout)
                base = base or []  # no verdict on the old text: remove less, never more
            seen = {diagnostic_key(d) for d in shift_all(base, line_map(as_synced(before), synced))}
            errors = [d for d in errors if diagnostic_key(d) not in seen]
        self._remember(path, synced, verdict)
        await self._resync(client, path, language)
        return errors

    async def _resync(self, client: LspClient, path: str, language: str) -> None:
        newest = self._newest.get(path)
        try:
            if newest is None:
                await client.sync(path, language=language)
            elif as_synced(newest) != client.synced_text(path):
                await client.sync(path, newest, language=language)
        except Exception:
            log.debug("LSP resync of %s failed", path, exc_info=True)


# -- the installed checkers and who edits ---------------------------------------------------------
#
# Checkers are installed per tier key: the home of the profile whose language servers they run
# (``tier_key``; wave 45, SEC-14), so on a multiplex host one profile's edits never reach a server
# started with another profile's child env or ``lsp.servers``. The key "" (``PROCESS_WIDE``) is a
# checker for every profile (a single-scope host, tests). Which tier an edit uses: the key bound
# for the running tool call (``LspToolHooks.before`` binds it from the call's scope), else the
# scope the code runs under (``foundation.scope``); with neither, the process-wide checker, else
# the only one installed.

PROCESS_WIDE = ""

_ACTIVE: dict[str, EditDiagnostics] = {}
_OWNER: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "indus_lsp_owner", default=(None, None)
)
_TIER_KEY: ContextVar[str | None] = ContextVar("indus_lsp_tier", default=None)
_EDITS: OrderedDict[tuple[str, str], list[str]] = OrderedDict()


def tier_key(scope: Scope | None) -> str | None:
    """The tier key of ``scope``: its profile home as an absolute path (None without a scope)."""
    return os.path.abspath(os.fspath(scope.home)) if scope is not None else None


def install_active(checker: EditDiagnostics | None, *, key: str | None = None) -> None:
    """Make ``checker`` the one consulted for edits under tier ``key`` (None there: remove that
    tier's). Without ``key``: ``checker`` replaces every installed one as the process-wide
    checker, and None turns LSP checks off for the whole process."""
    if key is None:
        _ACTIVE.clear()
        key = PROCESS_WIDE
    else:
        _ACTIVE.pop(key, None)
    if checker is not None:
        _ACTIVE[key] = checker


def installed() -> dict[str, EditDiagnostics]:
    """Every installed checker by tier key (a copy)."""
    return dict(_ACTIVE)


def bind_tier(scope: Scope | None) -> None:
    """Bind the tier of ``scope`` for the running tool call (no scope: unbind)."""
    _TIER_KEY.set(tier_key(scope))


def active(key: str | None = None) -> EditDiagnostics | None:
    """The checker for tier ``key`` (default: the bound tier, see the notes above)."""
    if key is None:
        key = _TIER_KEY.get() or tier_key(scope_or_none())
    if key is not None:
        return _ACTIVE.get(key) or _ACTIVE.get(PROCESS_WIDE)
    found = _ACTIVE.get(PROCESS_WIDE)
    if found is None and len(_ACTIVE) == 1:
        found = next(iter(_ACTIVE.values()))
    return found


async def release_folder(folder: str) -> int:
    """Stop this process's servers rooted at or below ``folder``, which is about to be removed (a
    worktree), in every installed tier (a folder can be open in more than one profile's servers);
    how many stopped. Nothing installed: 0. Never raises (wave 45)."""
    stopped = 0
    seen: set[int] = set()
    for checker in list(_ACTIVE.values()):
        if id(checker.manager) in seen:
            continue
        seen.add(id(checker.manager))
        try:
            stopped += await checker.manager.release(folder)
        except Exception:
            log.warning("releasing the language servers of %s failed", folder, exc_info=True)
    return stopped


def current_owner() -> tuple[str | None, str | None]:
    """``(owner, turn)`` bound for the running tool call."""
    return _OWNER.get()


def set_owner(owner: str | None, turn: str | None) -> Token[tuple[str | None, str | None]]:
    return _OWNER.set((owner or None, turn or None))


@contextlib.contextmanager
def bind_owner(owner: str | None, turn: str | None = None) -> Generator[None]:
    token = set_owner(owner, turn)
    try:
        yield
    finally:
        _OWNER.reset(token)


def record_edit(path: str) -> None:
    """Note that the bound owner edited ``path`` in the bound turn."""
    owner, turn = _OWNER.get()
    if not owner:
        return
    key = (owner, turn or "")
    edits = _EDITS.pop(key, None) or []
    full = os.path.abspath(path)
    if full not in edits and len(edits) < MAX_EDITS_PER_TURN:
        edits.append(full)
    _EDITS[key] = edits
    while len(_EDITS) > MAX_TURNS_KEPT:
        _EDITS.popitem(last=False)


def turn_edits(owner: str | None, turn: str | None) -> list[str]:
    """The files ``owner`` edited in ``turn``, in first-edit order."""
    if not owner:
        return []
    return list(_EDITS.get((owner, turn or ""), ()))


def forget_turn(owner: str | None, turn: str | None) -> None:
    if owner:
        _EDITS.pop((owner, turn or ""), None)


# -- the tool-call seam ---------------------------------------------------------------------------

NOTES_KEY = "lsp_diagnostics"


def _with_notes(result: ToolResult, notes: Sequence[str]) -> ToolResult:
    if not isinstance(result.content, str):
        return result
    text = result.content
    try:
        parsed: object = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        body = cast(dict[str, Any], parsed)
        body[NOTES_KEY] = [*cast(list[str], body.get(NOTES_KEY) or []), *notes]
        return ToolResult(json.dumps(body, ensure_ascii=False), result.is_error, result.error_type)
    joined = "\n\n".join(notes)
    return ToolResult(
        f"{text}\n\n[Language server, about earlier edits]\n{joined}",
        result.is_error,
        result.error_type,
    )


class LspToolHooks:
    """Wraps another hook seam: binds who is editing for each call and hands that owner's queued
    LSP notes to its next tool result."""

    def __init__(self, inner: ToolHooks = NO_HOOKS) -> None:
        self.inner = inner

    @property
    def empty(self) -> bool:
        return False

    @property
    def before_deadline_s(self) -> float | None:
        value = getattr(self.inner, "before_deadline_s", None)
        return float(value) if isinstance(value, int | float) else None

    @staticmethod
    def owner_of(ctx: ToolContext) -> str | None:
        return ctx.session_id or ctx.task_id

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None:
        set_owner(self.owner_of(ctx), ctx.turn_id)
        bind_tier(ctx.scope or scope_or_none())
        try:
            return await self.inner.before(call, ctx)
        except Exception:
            if self.inner.empty:
                return None
            raise

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None:
        await self.inner.after(call, ctx, result, duration_s)

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None:
        replaced: ToolResult | None = None
        try:
            replaced = await self.inner.transform(call, ctx, result)
        except Exception:
            log.debug("inner transform hook failed", exc_info=True)
        current = replaced if isinstance(replaced, ToolResult) else result
        checker = active(tier_key(ctx.scope))
        notes = checker.take_notes(self.owner_of(ctx)) if checker is not None else []
        if not notes:
            return replaced
        if not isinstance(current.content, str):
            owner = self.owner_of(ctx)
            if owner and checker is not None:  # a multimodal result keeps its shape: next time
                for index, note in enumerate(notes):
                    checker.post(owner, f"held back {index}", note, None)
            return replaced
        return _with_notes(current, notes)
