# pyright: strict
"""Shell hooks: the user's own commands on agent events, after consent (P13-19).

Config::

    hooks:
      pre_tool_call:
        - command: ~/.indus/agent-hooks/guard.sh
          matcher: "terminal|write_file"   # tool events only: regex, full match on the tool name
          timeout: 30                      # seconds (default 30, at most 600)
          fail_closed: false               # pre_tool_call only: a failed run blocks the tool
    hooks_auto_accept: false

Each configured command becomes one async callback on the P13-17 hooks bus (owner
``shell-hooks``), so it also gets the bus deadline and failure policy. A firing writes one JSON
object to the command's stdin (``hook_event_name``, ``session_id``, ``cwd``, ``tool_name``,
``tool_input``, ``tool_response``, ``prompt``, ``profile``, ``extra``; the Claude Code hook field
names plus the profile) and
reads an optional JSON answer from stdout, translated by ``interpret``:

- block (``pre_tool_call``): ``{"decision": "block", "reason"}``, ``{"action": "block",
  "message"}``, ``hookSpecificOutput.permissionDecision: "deny"``, or exit code 2 (stderr says why);
- ask a person: ``permissionDecision: "ask"`` or ``{"action": "approve"}``;
- modify: ``{"action": "modify", "args"}`` or ``hookSpecificOutput.updatedInput``;
- context (every other event): ``{"context"}``, ``additionalContext`` or
  ``hookSpecificOutput.additionalContext``.

Running: through the shell, in a new process group, with the P05 child environment (secrets and
``INDUS_*`` scrubbed); the group is killed on every way out (own timeout, the bus cancelling the
call, an error). ``pre_tool_call`` fails closed when the command cannot start (the shell's 126 /
127 included) or times out, and on any failure with ``fail_closed``; every other event fails open.

Consent: a command runs only once the person agreed to that (event, command) pair. Grants live
in ``<home>/hooks_consent.json``, keyed by ``sha256(event NUL command)``; the first 12 hex digits
are the grant id. ``INDUS_ACCEPT_HOOKS``, ``hooks_auto_accept: true`` or ``accept=True`` grant
(and record) without asking. Only interactive surfaces ask; the others skip unconsented hooks with
a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from indus.foundation import fsio
from indus.foundation.scope import Scope
from indus.plugins.context import current_call
from indus.plugins.hooks import HookBus, Registration
from indus.safety.child_env import child_env

__all__ = [
    "ACCEPT_ENV",
    "BLOCK_EXIT_CODE",
    "CONSENT_FILE",
    "DEFAULT_TIMEOUT_S",
    "EVENTS",
    "OWNER",
    "ConsentStore",
    "Grant",
    "HookRun",
    "HookSpec",
    "ShellHookReport",
    "accept_from",
    "build_payload",
    "configured_hooks",
    "interpret",
    "parse_hooks",
    "register_shell_hooks",
    "run_hook",
]

log = logging.getLogger(__name__)

EVENTS = ("pre_tool_call", "post_tool_call", "pre_llm_call", "on_session_start", "on_session_end")
TOOL_EVENTS = frozenset({"pre_tool_call", "post_tool_call"})
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 600.0
BLOCK_EXIT_CODE = 2
# What the shell exits with when the command could not be run at all (not executable, not found).
NOT_STARTED_CODES = frozenset({126, 127})
OUTPUT_CAP = 64 * 1024
MESSAGE_CAP = 500
CONSENT_FILE = "hooks_consent.json"
ACCEPT_ENV = "INDUS_ACCEPT_HOOKS"
OWNER = "shell-hooks"
DEFAULT_BLOCK = "Blocked by a shell hook."
_TRUE = frozenset({"1", "true", "yes", "on"})
# Keyword names the payload carries at the top level; the rest go under ``extra``.
_TOP_LEVEL = frozenset({"session_id", "cwd", "tool_name", "args", "result", "user_message"})
_NEVER_SENT = frozenset({"conversation_history"})


# -- config -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HookSpec:
    """One configured hook."""

    event: str
    command: str
    matcher: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    fail_closed: bool = False

    @property
    def digest(self) -> str:
        return hashlib.sha256(f"{self.event}\0{self.command}".encode()).hexdigest()

    @property
    def grant_id(self) -> str:
        return self.digest[:12]

    def matches(self, tool_name: object) -> bool:
        """Does this hook fire for ``tool_name``? Non-tool events always do."""
        if self.event not in TOOL_EVENTS or not self.matcher or self.matcher == "*":
            return True
        if not isinstance(tool_name, str):
            return False
        try:
            return re.fullmatch(self.matcher, tool_name) is not None
        except re.error:
            return tool_name == self.matcher


def _timeout(raw: object, where: str, warnings: list[str]) -> float:
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        if isinstance(raw, bool):
            raise TypeError
        value = float(cast(Any, raw))
    except (TypeError, ValueError):
        warnings.append(f"{where}.timeout is not a number; using {DEFAULT_TIMEOUT_S:g} s")
        return DEFAULT_TIMEOUT_S
    if value != value or value < 1:
        warnings.append(f"{where}.timeout must be at least 1 s; using {DEFAULT_TIMEOUT_S:g} s")
        return DEFAULT_TIMEOUT_S
    if value > MAX_TIMEOUT_S:
        warnings.append(f"{where}.timeout is above {MAX_TIMEOUT_S:g} s; clamped")
        return MAX_TIMEOUT_S
    return value


def _entry(event: str, index: int, raw: object, warnings: list[str]) -> HookSpec | None:
    where = f"hooks.{event}[{index}]"
    if not isinstance(raw, Mapping):
        warnings.append(f"{where} must be a mapping with a command; skipped")
        return None
    item = cast(Mapping[str, Any], raw)
    command = item.get("command")
    if not isinstance(command, str) or not command.strip():
        warnings.append(f"{where} has no command; skipped")
        return None
    matcher = item.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        warnings.append(f"{where}.matcher must be text; ignored")
        matcher = None
    if isinstance(matcher, str):
        matcher = matcher.strip() or None
    if matcher is not None and event not in TOOL_EVENTS:
        warnings.append(f"{where}.matcher only applies to tool events; ignored")
        matcher = None
    fail_closed = item.get("fail_closed", False)
    if not isinstance(fail_closed, bool):
        warnings.append(f"{where}.fail_closed must be true or false; using false")
        fail_closed = False
    if fail_closed and event != "pre_tool_call":
        warnings.append(f"{where}.fail_closed only applies to pre_tool_call; ignored")
        fail_closed = False
    return HookSpec(
        event, command.strip(), matcher, _timeout(item.get("timeout"), where, warnings), fail_closed
    )


def parse_hooks(raw: object) -> tuple[list[HookSpec], list[str]]:
    """The hooks in a ``hooks`` config value, and a warning per entry that was skipped."""
    warnings: list[str] = []
    if raw is None:
        return [], warnings
    if not isinstance(raw, Mapping):
        return [], ["hooks must be a mapping of event names to lists; ignored"]
    specs: list[HookSpec] = []
    for event, entries in cast(Mapping[object, object], raw).items():
        name = str(event)
        if name not in EVENTS:
            warnings.append(
                f"hooks.{name}: not an event shell hooks can attach to (one of {', '.join(EVENTS)})"
            )
            continue
        if entries is None:
            continue
        if not isinstance(entries, list):
            warnings.append(f"hooks.{name} must be a list of hooks; skipped")
            continue
        for index, item in enumerate(cast(list[object], entries)):
            spec = _entry(name, index, item, warnings)
            if spec is not None:
                specs.append(spec)
    return specs, warnings


def _setting(scope: Scope, key: str) -> object:
    if scope.config is None:
        return None
    try:
        return scope.config.get(key)
    except Exception:
        log.debug("%s could not be read", key, exc_info=True)
        return None


def configured_hooks(scope: Scope) -> tuple[list[HookSpec], list[str]]:
    """The scope's configured hooks and the parse warnings."""
    return parse_hooks(_setting(scope, "hooks"))


def accept_from(scope: Scope, accept: bool = False) -> bool:
    """True when hooks are accepted without asking: the caller's flag, ``INDUS_ACCEPT_HOOKS`` or
    ``hooks_auto_accept``."""
    if accept or scope.env.get(ACCEPT_ENV, "").strip().lower() in _TRUE:
        return True
    value = _setting(scope, "hooks_auto_accept")
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    return value is True


# -- consent ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    digest: str
    event: str
    command: str
    granted_at: str
    script_mtime: float | None = None

    @property
    def id(self) -> str:
        return self.digest[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "hash": self.digest,
            "event": self.event,
            "command": self.command,
            "granted_at": self.granted_at,
            "script_mtime": self.script_mtime,
        }


def script_path(command: str) -> Path | None:
    """The file a command starts, when its first word names one (``~`` expanded)."""
    first = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    if not first or ("/" not in first and not first.startswith("~") and "\\" not in first):
        return None
    return Path(os.path.expanduser(first))


def script_mtime(command: str) -> float | None:
    path = script_path(command)
    try:
        return path.stat().st_mtime if path is not None else None
    except OSError:
        return None


class ConsentStore:
    """``<home>/hooks_consent.json``: which (event, command) pairs the person agreed to."""

    def __init__(self, home: Path) -> None:
        self.path = home / CONSENT_FILE

    def _lock(self) -> fsio.FileLock:
        return fsio.FileLock(self.path.with_name(self.path.name + ".lock"))

    def read(self) -> tuple[dict[str, Grant], bool]:
        """(grants by hash, readable). A missing file is readable and empty; a broken one is not
        readable, so nothing in it counts and it is never overwritten."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}, True
        except (OSError, ValueError, UnicodeDecodeError):
            log.warning("%s cannot be read; no shell hook counts as approved", self.path)
            return {}, False
        if not isinstance(raw, Mapping) or not isinstance(
            cast(Mapping[str, Any], raw).get("grants"), list
        ):
            log.warning("%s is not a consent file; no shell hook counts as approved", self.path)
            return {}, False
        grants: dict[str, Grant] = {}
        for item in cast(list[object], cast(Mapping[str, Any], raw)["grants"]):
            if not isinstance(item, Mapping):
                continue
            entry = cast(Mapping[str, Any], item)
            digest, event, command = entry.get("hash"), entry.get("event"), entry.get("command")
            if not (
                isinstance(digest, str) and isinstance(event, str) and isinstance(command, str)
            ):
                continue
            if HookSpec(event, command).digest != digest:
                continue  # a hand-edited entry that no longer matches its pair counts for nothing
            mtime = entry.get("script_mtime")
            grants[digest] = Grant(
                digest,
                event,
                command,
                str(entry.get("granted_at") or ""),
                float(mtime) if isinstance(mtime, int | float) else None,
            )
        return grants, True

    def granted(self, spec: HookSpec) -> Grant | None:
        return self.read()[0].get(spec.digest)

    def _write(self, grants: Mapping[str, Grant]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "grants": [g.to_dict() for g in grants.values()]}
        fsio.atomic_write_json(self.path, data, mode=0o600)

    def grant(self, spec: HookSpec, *, now: datetime | None = None) -> Grant:
        """Record consent for ``spec`` (replacing an older grant of the same pair)."""
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        made = Grant(
            spec.digest,
            spec.event,
            spec.command,
            moment.isoformat(timespec="seconds"),
            script_mtime(spec.command),
        )
        with self._lock():
            grants, readable = self.read()
            if not readable:
                log.warning("not saving the hook approval: %s is unreadable", self.path)
                return made
            grants[spec.digest] = made
            self._write(grants)
        return made

    def revoke(self, key: str) -> list[Grant]:
        """Drop the grants whose id starts with ``key`` (at least 4 hex chars) or whose command is
        ``key``; returns what was dropped."""
        wanted = key.strip()
        with self._lock():
            grants, readable = self.read()
            if not readable or not wanted:
                return []
            by_id = len(wanted) >= 4 and all(c in "0123456789abcdef" for c in wanted.lower())
            gone = [
                g
                for g in grants.values()
                if g.command == wanted or (by_id and g.digest.startswith(wanted.lower()))
            ]
            if gone:
                for g in gone:
                    grants.pop(g.digest, None)
                self._write(grants)
        return gone


# -- one run ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HookRun:
    """How one run of a hook command went."""

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None
    elapsed_s: float = 0.0


def _json_safe(value: object) -> object:
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        return str(value)


def build_payload(
    event: str, kwargs: Mapping[str, Any], cwd: str = "", profile: str = ""
) -> dict[str, Any]:
    """The stdin object for ``event`` fired with ``kwargs`` (``profile``: the firing profile)."""
    payload: dict[str, Any] = {
        "hook_event_name": event,
        "session_id": str(kwargs.get("session_id") or ""),
        "cwd": str(kwargs.get("cwd") or cwd or ""),
        "profile": profile,
    }
    if event in TOOL_EVENTS:
        payload["tool_name"] = kwargs.get("tool_name")
        args = kwargs.get("args")
        payload["tool_input"] = (
            _json_safe(dict(cast(Mapping[str, Any], args))) if isinstance(args, Mapping) else None
        )
        if event == "post_tool_call":
            payload["tool_response"] = _json_safe(kwargs.get("result"))
    if event == "pre_llm_call":
        payload["prompt"] = str(kwargs.get("user_message") or "")
    payload["extra"] = {
        k: _json_safe(v) for k, v in kwargs.items() if k not in _TOP_LEVEL and k not in _NEVER_SENT
    }
    return payload


def _kill_group(proc: asyncio.subprocess.Process, env: Mapping[str, str]) -> None:
    """Kill the hook's process group (POSIX: also after a normal exit, for children it left)."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                env=dict(env),
                capture_output=True,
                timeout=10,
                check=False,
            )
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()


async def _read_capped(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    kept = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if len(kept) < OUTPUT_CAP:
            kept.extend(chunk[: OUTPUT_CAP - len(kept)])
    return kept.decode("utf-8", "replace")


async def run_hook(
    spec: HookSpec,
    payload: Mapping[str, Any],
    *,
    scope: Scope,
    cwd: str | None = None,
    session_id: str | None = None,
) -> HookRun:
    """Run ``spec.command`` once with ``payload`` on stdin (see the module notes)."""
    env = child_env(scope, session_id=session_id or None)
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    workdir = cwd if cwd and Path(cwd).is_dir() else None
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            spec.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workdir,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as exc:
        return HookRun(error=f"could not start: {exc}")
    try:

        async def talk() -> tuple[str, str, int]:
            stdin = proc.stdin
            if stdin is not None:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    stdin.write(data)
                    await stdin.drain()
                stdin.close()
            out, err = await asyncio.gather(_read_capped(proc.stdout), _read_capped(proc.stderr))
            code = await proc.wait()
            return out, err, code

        try:
            out, err, code = await asyncio.wait_for(talk(), timeout=spec.timeout_s)
        except TimeoutError:
            return HookRun(timed_out=True, elapsed_s=time.monotonic() - started)
        return HookRun(code, out, err, elapsed_s=time.monotonic() - started)
    finally:
        # Every way out (timeout, the bus cancelling us, an error, a normal end that left
        # background children in the group): the whole group goes.
        _kill_group(proc, env)


# -- answers ------------------------------------------------------------------------------------


def _clip(text: str) -> str:
    return text[:MESSAGE_CAP]


def _text(value: object) -> str | None:
    return value.strip()[:MESSAGE_CAP] if isinstance(value, str) and value.strip() else None


def _specific(data: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = data.get("hookSpecificOutput")
    return cast(Mapping[str, Any], inner) if isinstance(inner, Mapping) else {}


def _tool_answer(data: Mapping[str, Any]) -> dict[str, Any] | None:
    specific = _specific(data)
    decision = specific.get("permissionDecision")
    if data.get("decision") == "block" or data.get("action") == "block" or decision == "deny":
        message = (
            _text(data.get("reason"))
            or _text(data.get("message"))
            or _text(specific.get("permissionDecisionReason"))
            or DEFAULT_BLOCK
        )
        return {"action": "block", "message": message}
    updated = specific.get("updatedInput")
    if data.get("action") == "modify" and isinstance(data.get("args"), Mapping):
        return {"action": "modify", "args": dict(cast(Mapping[str, Any], data["args"]))}
    if isinstance(updated, Mapping):
        return {"action": "modify", "args": dict(cast(Mapping[str, Any], updated))}
    if data.get("action") == "approve" or decision == "ask":
        answer: dict[str, Any] = {"action": "approve"}
        message = _text(data.get("message")) or _text(specific.get("permissionDecisionReason"))
        if message:
            answer["message"] = message
        rule = _text(data.get("rule_key"))
        if rule:
            answer["rule_key"] = rule
        return answer
    return None


def _context_answer(data: Mapping[str, Any]) -> dict[str, Any] | None:
    for value in (
        data.get("context"),
        data.get("additionalContext"),
        _specific(data).get("additionalContext"),
    ):
        if isinstance(value, str) and value.strip():
            return {"context": value.strip()}
    return None


def _parse_stdout(spec: HookSpec, stdout: str) -> tuple[dict[str, Any] | None, bool]:
    """(answer, stdout was a JSON object)."""
    text = stdout.strip()
    if not text:
        return None, True
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("shell hook %s (%s): stdout is not JSON; ignored", spec.grant_id, spec.event)
        return None, False
    if not isinstance(data, Mapping):
        return None, False
    found = cast(Mapping[str, Any], data)
    if spec.event == "pre_tool_call":
        return _tool_answer(found), True
    if spec.event == "post_tool_call":
        return None, True
    return _context_answer(found), True


def _closed(spec: HookSpec, why: str) -> dict[str, Any]:
    return {"action": "block", "message": f"Tool blocked: shell hook {spec.command!r} {why}"}


def interpret(spec: HookSpec, run: HookRun) -> dict[str, Any] | None:
    """The bus answer for one run (block / approve / modify / context), or None."""
    guard = spec.event == "pre_tool_call"
    if run.error is None and run.exit_code in NOT_STARTED_CODES:
        detail = _clip(run.stderr.strip()) or "command not found"
        run = HookRun(error=f"could not start (exit {run.exit_code}: {detail})")
    if run.error is not None or run.timed_out:
        why = run.error or f"did not finish within {spec.timeout_s:g} s"
        log.warning("shell hook %s (%s %s) %s", spec.grant_id, spec.event, spec.command, why)
        return _closed(spec, why) if guard else None
    stderr = run.stderr.strip()
    if guard and run.exit_code == BLOCK_EXIT_CODE:
        answer, _ = _parse_stdout(spec, run.stdout)
        if answer is not None and answer.get("action") == "block":
            return answer
        return {"action": "block", "message": stderr[:MESSAGE_CAP] or DEFAULT_BLOCK}
    if run.exit_code not in (0, None):
        log.warning(
            "shell hook %s (%s) exited %s: %s",
            spec.grant_id,
            spec.event,
            run.exit_code,
            stderr[:MESSAGE_CAP],
        )
    answer, was_json = _parse_stdout(spec, run.stdout)
    if answer is None and spec.fail_closed and (run.exit_code not in (0, None) or not was_json):
        return _closed(spec, f"failed (exit {run.exit_code}) without a decision")
    return answer


# -- registering ----------------------------------------------------------------------------------


@dataclass
class ShellHookReport:
    registered: list[HookSpec] = field(default_factory=list[HookSpec])
    skipped: list[tuple[HookSpec, str]] = field(default_factory=list[tuple[HookSpec, str]])
    warnings: list[str] = field(default_factory=list[str])
    handles: list[Registration] = field(default_factory=list[Registration])


Callback = Callable[..., Awaitable[dict[str, Any] | None]]


def make_callback(spec: HookSpec, scope: Scope, cwd: str | None = None) -> Callback:
    """The bus callback for ``spec``."""

    async def fire(**kwargs: Any) -> dict[str, Any] | None:
        if spec.event in TOOL_EVENTS and not spec.matches(kwargs.get("tool_name")):
            return None
        site = current_call()
        where = str(kwargs.get("cwd") or (site.cwd if site is not None else "") or cwd or "")
        session = str(kwargs.get("session_id") or "") or None
        run = await run_hook(
            spec,
            build_payload(spec.event, kwargs, where, scope.profile_id),
            scope=scope,
            cwd=where,
            session_id=session,
        )
        return interpret(spec, run)

    label = f"shell hook {spec.grant_id} [{spec.event}] {spec.command}"
    fire.__name__ = fire.__qualname__ = label
    return fire


Ask = Callable[[HookSpec], bool]


def register_shell_hooks(
    bus: HookBus,
    scope: Scope,
    *,
    interactive: bool,
    surface: str = "",
    accept: bool = False,
    ask: Ask | None = None,
    cwd: str | None = None,
    store: ConsentStore | None = None,
) -> ShellHookReport:
    """Register every configured hook the person agreed to on ``bus``.

    ``interactive`` surfaces call ``ask(spec)`` for a hook without consent (a yes is recorded);
    the others skip it with a warning. ``accept`` (or ``INDUS_ACCEPT_HOOKS`` /
    ``hooks_auto_accept``) records consent without asking.
    """
    report = ShellHookReport()
    specs, warnings = configured_hooks(scope)
    for text in warnings:
        log.warning("%s", text)
    report.warnings.extend(warnings)
    if not specs:
        return report
    consents = store if store is not None else ConsentStore(scope.home)
    grants, _ = consents.read()
    auto = accept_from(scope, accept)
    where = surface or "this surface"
    for spec in specs:
        consented = spec.digest in grants
        if not consented and auto:
            consents.grant(spec)
            consented = True
            log.info("shell hook %s accepted without asking: %s", spec.grant_id, spec.command)
        elif not consented and interactive and ask is not None:
            try:
                consented = bool(ask(spec))
            except Exception:
                log.warning("asking about shell hook %s failed", spec.grant_id, exc_info=True)
                consented = False
            if consented:
                consents.grant(spec)
        if not consented:
            reason = (
                "not approved"
                if interactive
                else f"not approved, and {where} never asks (approve it in an interactive session "
                f"or with `indus hooks test {spec.event} --accept-hooks`)"
            )
            log.warning(
                "shell hook %s for %s skipped: %s (%s)",
                spec.grant_id,
                spec.event,
                reason,
                spec.command,
            )
            report.skipped.append((spec, reason))
            continue
        handle = bus.register(spec.event, make_callback(spec, scope, cwd), owner=OWNER)
        report.handles.append(handle)
        report.registered.append(spec)
    return report


def consent_text(spec: HookSpec) -> str:
    """What a person is shown before agreeing to a hook."""
    return (
        f"A shell hook wants to run on {spec.event}:\n"
        f"    {spec.command}\n"
        "It runs with your user rights (secrets are kept out of its environment). Only allow "
        "commands you trust."
    )


def unregister(bus: HookBus) -> int:
    """Remove every shell hook from ``bus`` (config reload)."""
    return bus.drop_owner(OWNER)
