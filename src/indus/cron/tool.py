"""``cronjob_manage``: the agent's one tool for scheduled jobs (H15, P14-18).

``action`` is one of ``create``, ``list``, ``update``, ``pause``, ``resume``, ``remove``, ``run``.
Jobs are stored and run through ``ports.CronBackend``; this module checks what the model asks for:

- inference pins (``model``, ``provider``, ``base_url``, ``reasoning_effort``) belong to the user
  and are refused here (``indus cron edit`` sets them); ``pinned=true`` copies the current main
  model and provider onto the job and is only for when the user asked, ``pinned=false`` lets the
  job follow the main model again;
- prompts (create, update and the per-run context of ``run``) go through the P05 threat scan
  (injection, exfiltration, hidden characters) and a job that would stop or restart the Indus
  gateway is refused, since the gateway runs the job (restart-loop guard);
- ``script`` is a file name under ``<home>/scripts/`` that exists; ``monitor`` is one field (an
  http(s) URL or a script name), never with ``no_agent``;
- ``deliver``/``failure_deliver`` must parse; from inside a cron run ``origin`` is pinned to the
  parent job's concrete target, or ``local`` (no dangling origins);
- ``run`` returns a handle at once when the session can take a detached result (a result sink is
  registered for it); the outcome then arrives as an internal message. Otherwise it runs inline.
  A job already running is refused.

Availability: interactive sessions (CLI, TUI, gateway chats; ``run_context`` ``interactive`` or
unset); cron runs only with ``cron.allow_agent_scheduling`` (R07.15); one-shot and other
unattended runs never. ``base_url_problem`` is the R01.11 rule for the user-facing CLI.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from indus.cron import monitor, ports, targets
from indus.cron.ports import JobAmbiguous, JobNotFound, RunReport, StoreUnavailable
from indus.foundation.scope import Scope, scope_or_none
from indus.safety import threat_scan
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "ACTIONS",
    "SCHEMA",
    "TOOL_NAME",
    "TOOLSET",
    "USER_ONLY_FIELDS",
    "base_url_problem",
    "cronjob_manage",
    "lifecycle_problem",
    "prompt_problem",
    "register_result_sink",
    "register_tools",
    "script_problem",
]

log = logging.getLogger(__name__)

TOOL_NAME = "cronjob_manage"
TOOLSET = "cronjob"
ACTIONS = ("create", "list", "update", "pause", "resume", "remove", "run")
USER_ONLY_FIELDS = ("model", "provider", "base_url", "reasoning_effort")
SELF_REF = "self"
_SCAN_SCOPE: threat_scan.Scope = "cron"
_SCRIPT_READ_LIMIT = 256 * 1024
_CRON_SESSION = re.compile(r"^cron_(?P<job>[0-9a-f]{12})_\d{8}_\d{6}$")

_LIFECYCLE = re.compile(
    r"\bindus\b[^\n]{0,60}?\bgateway\s+(?:restart|stop|kill)\b"
    r"|\bsystemctl\b[^\n]{0,120}\b(?:restart|stop|kill|try-restart|reload-or-restart)\b"
    r"[^\n]{0,120}\bindus[-_.]?gateway"
    r"|\blaunchctl\b[^\n]{0,120}\b(?:kickstart|stop|unload|bootout|kill|remove)\b"
    r"[^\n]{0,120}\bai\.indus\.gateway"
    r"|\b(?:pkill|killall)\b[^\n]{0,60}\bindus[- _]?gateway",
    re.IGNORECASE,
)

_DESCRIPTION = (
    "Schedule and look after jobs that run on their own later or on a repeat. "
    "action='create' needs `schedule` and a `prompt` (or `skills`, or a `script` with "
    "no_agent=true); 'list' shows the jobs; 'update', 'pause', 'resume', 'remove' and 'run' take "
    "`job_id` (an id or a job name — list first, never guess). 'run' starts the job now and "
    "returns at once; its result comes back to this conversation by itself, so do not wait or "
    "check. Each run is a fresh session that knows nothing of this chat: write prompts that stand "
    "on their own. The job's final answer is what gets delivered. Jobs use the user's main model "
    "unless the user pinned one. Change an existing job rather than creating a near copy."
)


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _list(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _bool(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _script_text(where: str) -> str:
    return (
        f"File name of a script in {where} (create it there first). With no_agent=true its "
        "output is the whole job; otherwise it runs before the agent and its output becomes "
        "context. '' clears it on update."
    )


SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": _DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS), "description": "What to do."},
            "job_id": _str("The job's id or name (update, pause, resume, remove, run)."),
            "schedule": _str(
                "When it runs. Repeating: '30m' or 'every 2h' (every 30 minutes / 2 hours), "
                "'weekdays at 9am', 'every monday 7:30', 'every day at noon', or five cron fields "
                "'0 9 * * 1-5'. Once: 'in 45m' (do not work out a timestamp yourself) or an ISO "
                "time '2026-11-01T09:00'."
            ),
            "prompt": _str(
                "create/update: the complete instruction the job runs with. run: extra context "
                "for this one run only (not saved)."
            ),
            "name": _str("A short name for the job."),
            "repeat": {
                "type": "integer",
                "description": "How many runs before the job ends. Leave out: once for one-time "
                "schedules, forever for repeating ones.",
            },
            "deliver": _str(
                "Where the result goes: 'local' (saved only), 'origin' (this chat), 'all', a "
                "platform name (its home channel) or 'platform:chat_id[:thread_id]'; comma "
                "separated for several. Leave out for this chat."
            ),
            "failure_deliver": _str(
                "Where failure notices go instead of `deliver` ('local' keeps them quiet). "
                "'' clears it on update."
            ),
            "skills": _list("Skills loaded before the prompt, in order. [] clears on update."),
            "script": _str(_script_text("the profile's scripts folder")),
            "monitor": _str(
                "Optional change detector run before the agent: an http(s) URL or a script file "
                "name. Same output as last time skips the run; changed output wakes the agent "
                "with the difference. Output must not contain timestamps. '' clears it."
            ),
            "no_agent": _bool(
                "true = no model at all: the script's output is delivered as is (empty output "
                "sends nothing; a failing script sends an alert). Needs `script`."
            ),
            "context_from": _list(
                "Ids of other jobs whose latest result is added as context each run. [] clears."
            ),
            "continuity": _bool(
                "true = each run also sees this job's own previous result (to continue or "
                "avoid repeats)."
            ),
            "enabled_toolsets": _list(
                "Toolsets the job's agent may use (for example ['web', 'terminal']). [] means "
                "no tools at all."
            ),
            "workdir": _str(
                "Absolute path of an existing folder to work in; its project files are loaded. "
                "'' clears it."
            ),
            "paused": _bool("create only: store the job paused (resume or run it later)."),
            "paused_reason": _str("create only, with paused=true: why it is paused."),
            "pinned": _bool(
                "Only when the user asks: true keeps the job on today's main model, false lets "
                "it follow the main model again."
            ),
        },
        "required": ["action"],
    },
}


def _schema_overrides(scope: Scope | None) -> Mapping[str, Any] | None:
    if scope is None:
        return None
    params = json.loads(json.dumps(SCHEMA["parameters"]))
    params["properties"]["script"]["description"] = _script_text(str(ports.scripts_dir(scope)))
    return {"parameters": params}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME,
        TOOLSET,
        SCHEMA,
        cronjob_manage,
        is_async=True,
        schema_overrides=_schema_overrides,
        traits=ToolTraits(mutating=True),
    )


# -- detached results ---------------------------------------------------------------------------

_lock = threading.Lock()
_sinks: dict[str, Callable[[str], object]] = {}
_running: set[tuple[str, str]] = set()
_tasks: set[asyncio.Task[None]] = set()


def register_result_sink(session_id: str, fn: Callable[[str], object]) -> Callable[[], None]:
    """Where ``run`` results for ``session_id`` go (the host queues them as an internal message
    between turns); returns the undo."""
    with _lock:
        _sinks[session_id] = fn

    def undo() -> None:
        with _lock:
            if _sinks.get(session_id) is fn:
                del _sinks[session_id]

    return undo


def _sink_for(session_id: str | None) -> Callable[[str], object] | None:
    if not session_id:
        return None
    with _lock:
        return _sinks.get(session_id)


# -- checks ---------------------------------------------------------------------------------------


def prompt_problem(text: str | None) -> str | None:
    """Why this prompt text is refused (threat scan, restart-loop guard), or None."""
    if not text:
        return None
    hits = threat_scan.scan(text, scope=_SCAN_SCOPE)
    if hits:
        return (
            "the prompt was refused by the safety scan (" + ", ".join(hits) + "): scheduled "
            "prompts may not carry instruction overrides, secret exfiltration or hidden characters"
        )
    return lifecycle_problem(text)


def lifecycle_problem(text: str | None) -> str | None:
    if text and _LIFECYCLE.search(text):
        return (
            "a scheduled job may not stop or restart the Indus gateway: the gateway runs the job, "
            "so it would cut itself off (and restart again on every run). Do that from a shell."
        )
    return None


def script_problem(scope: Scope, name: str | None) -> str | None:
    """Why ``name`` cannot be a job script, or None (an empty name clears the script)."""
    raw = (name or "").strip()
    if not raw:
        return None
    folder = ports.scripts_dir(scope)
    if raw.startswith(("/", "~", "\\")) or (len(raw) > 1 and raw[1] == ":"):
        return f"give the script as a file name inside {folder}/, not a path ({raw!r})"
    if any(ch.isspace() for ch in raw) or any(ch in raw for ch in ";|&$`<>()"):
        return (
            f"{raw!r} looks like a command line; put the commands in a script file in {folder}/ "
            "and give its name"
        )
    try:
        root = folder.resolve()
        resolved = (folder / raw).resolve()
    except OSError as exc:
        return f"the script path cannot be resolved: {exc}"
    if not resolved.is_relative_to(root):
        return f"{raw!r} leaves the scripts folder {folder}/"
    if not resolved.is_file():
        return f"no script {raw!r} in {folder}/; write it there first"
    try:
        with resolved.open("rb") as handle:
            body = handle.read(_SCRIPT_READ_LIMIT).decode("utf-8", "replace")
    except OSError:
        return None
    return lifecycle_problem(body)


def workdir_problem(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        return f"the workdir must be an absolute path (got {text!r})"
    if not path.is_dir():
        return f"the workdir {text!r} is not an existing folder"
    return None


def deliver_problem(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = targets.parse_targets(value)
    if parsed.ok:
        return None
    return "; ".join(p.message for p in parsed.problems)


def base_url_problem(scope: Scope, provider: str | None, base_url: str | None) -> str | None:
    """R01.11: a per-job ``base_url`` needs an explicit provider whose configured endpoint has the
    same scheme, host and port, so a stored key is never sent to another server."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return None
    name = (provider or "").strip()
    if not name:
        return "a custom base_url needs an explicit provider (--provider)"
    from indus.providers.hosts import try_parse
    from indus.providers.registry import Registry

    wanted = try_parse(url)
    if wanted is None:
        return f"{url!r} is not a usable base URL"
    try:
        profile = Registry(scope).lookup(name)
    except Exception:
        profile = None
    if profile is None:
        return f"unknown provider {name!r}; a base_url can only narrow a configured provider"
    configured = try_parse(profile.base_url) if profile.base_url else None
    if configured is None or configured.origin != wanted.origin:
        where = profile.base_url or "none configured"
        return (
            f"{url!r} is not where provider {name!r} is configured to send its key ({where}); "
            "the base URL must keep the same scheme, host and port"
        )
    return None


def _main_route(scope: Scope) -> tuple[str, str]:
    from indus.providers import route as route_mod

    resolved = route_mod.resolve(scope)
    return resolved.model, resolved.provider


MAIN_ROUTE: Callable[[Scope], tuple[str, str]] = _main_route
"""The current main model and provider (tests replace it)."""


def availability_problem(scope: Scope, run_context: object) -> str | None:
    context = run_context if isinstance(run_context, str) else None
    if context in (None, "", "interactive"):
        return None
    if context == "cron":
        if ports.flag_setting(scope, "allow_agent_scheduling", False):
            return None
        return (
            "scheduled runs may not create or change cron jobs (cron.allow_agent_scheduling is "
            "off); report what should be scheduled in the answer instead"
        )
    return "cron jobs can only be managed from an interactive session"


# -- views ----------------------------------------------------------------------------------------


def job_view(job: Any) -> dict[str, Any]:
    times, done = ports.repeat_of(job)
    view: dict[str, Any] = {
        "id": ports.field(job, "id"),
        "name": ports.field(job, "name"),
        "schedule": ports.field(job, "schedule_display") or ports.schedule_kind(job),
        "state": ports.field(job, "state"),
        "enabled": bool(ports.field(job, "enabled", True)),
        "next_run_at": ports.field(job, "next_run_at"),
        "deliver": ports.field(job, "deliver", "local"),
        "repeat": f"{done}/{times}" if times else f"{done}/forever",
    }
    prompt = str(ports.field(job, "prompt", "") or "")
    if prompt:
        view["prompt"] = prompt if len(prompt) <= 200 else prompt[:197] + "..."
    for key in (
        "skills",
        "script",
        "monitor_script",
        "monitor_url",
        "workdir",
        "context_from",
        "enabled_toolsets",
        "failure_deliver",
        "last_run_at",
        "last_status",
        "last_error",
        "last_delivery_error",
        "paused_reason",
        "model",
        "provider",
    ):
        value = ports.field(job, key)
        if value not in (None, "", []):
            view[key] = value
    if ports.field(job, "no_agent"):
        view["no_agent"] = True
    streak = int(ports.field(job, "failure_streak", 0) or 0)
    if streak:
        view["failure_streak"] = streak
    return view


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, indent=2, default=str)


def _fail(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": message, **extra}, indent=2, default=str)


# -- argument helpers -----------------------------------------------------------------------------


def _clean_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list | tuple):
        return [str(v).strip() for v in value if str(v).strip()]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return None


def _with_continuity(refs: list[str] | None, continuity: object) -> list[str] | None:
    if not isinstance(continuity, bool):
        return refs
    out = [r for r in (refs or []) if r.lower() != SELF_REF]
    if continuity:
        out.append(SELF_REF)
    return out


def _context_problem(backend: ports.CronBackend, refs: list[str] | None) -> str | None:
    for ref in refs or []:
        if ref.lower() == SELF_REF:
            continue
        try:
            backend.resolve(ref)
        except JobAmbiguous as exc:
            return str(exc)
        except JobNotFound:
            return f"context_from names {ref!r}, which is not a job; list the jobs first"
    return None


def _parent_target(
    backend: ports.CronBackend, session_id: str | None
) -> targets.DeliveryTarget | None:
    match = _CRON_SESSION.match(session_id or "")
    if match is None:
        return None
    parent = backend.get(match.group("job"))
    if parent is None:
        return None
    return targets.first_concrete(ports.field(parent, "deliver"), ports.origin_of(parent))


def _deliver_value(
    raw: object,
    *,
    backend: ports.CronBackend,
    run_context: object,
    session_id: str | None,
    origin: Mapping[str, Any] | None,
) -> str | None:
    """The ``deliver`` text to store; None means "leave it" on update."""
    value = None if raw is None else str(raw)
    if run_context == "cron":
        return targets.pin_origin(value, _parent_target(backend, session_id))
    if value is None:
        return targets.ORIGIN if origin else None
    return targets.join_tokens(targets.split_tokens(value)) or targets.LOCAL


# -- actions --------------------------------------------------------------------------------------


class _Refused(Exception):
    pass


def _check(problem: str | None) -> None:
    if problem:
        raise _Refused(problem)


def _common_fields(
    scope: Scope,
    backend: ports.CronBackend,
    args: Mapping[str, Any],
    job: Any | None,
) -> dict[str, Any]:
    """Validated authored fields present in ``args`` (create and update share these)."""
    out: dict[str, Any] = {}
    if args.get("prompt") is not None:
        _check(prompt_problem(str(args["prompt"])))
        out["prompt"] = str(args["prompt"])
    for key in ("schedule", "name"):
        if args.get(key) is not None:
            out[key] = str(args[key]).strip()
    if args.get("repeat") is not None:
        try:
            out["repeat"] = int(args["repeat"])
        except (TypeError, ValueError):
            raise _Refused("repeat must be a whole number") from None
    skills = _clean_list(args.get("skills"))
    if skills is not None:
        out["skills"] = skills
    if args.get("script") is not None:
        script = str(args["script"]).strip()
        _check(script_problem(scope, script))
        out["script"] = script or None
    if args.get("no_agent") is not None:
        out["no_agent"] = bool(args["no_agent"])
    if args.get("monitor") is not None:
        mon_script, mon_url = monitor.split_monitor(str(args["monitor"]))
        if mon_script:
            _check(script_problem(scope, mon_script))
        out["monitor_script"], out["monitor_url"] = mon_script, mon_url
    no_agent = out.get("no_agent", bool(ports.field(job, "no_agent")) if job is not None else False)
    if "monitor_script" in out or "no_agent" in out:
        mon_script = out.get("monitor_script", ports.field(job, "monitor_script") if job else None)
        mon_url = out.get("monitor_url", ports.field(job, "monitor_url") if job else None)
        _check(monitor.validate_monitor(mon_script, mon_url, no_agent=no_agent))
    script_now = out.get("script", ports.field(job, "script") if job is not None else None)
    if no_agent and not script_now:
        raise _Refused(
            "no_agent=true needs a script: the script's output is the whole job (no model runs)"
        )
    refs = _clean_list(args.get("context_from"))
    if refs is not None or isinstance(args.get("continuity"), bool):
        current = refs if refs is not None else _clean_list(ports.field(job, "context_from"))
        merged = _with_continuity(current, args.get("continuity"))
        _check(_context_problem(backend, merged))
        out["context_from"] = merged or None
    toolsets = args.get("enabled_toolsets")
    if toolsets is not None:
        out["enabled_toolsets"] = _clean_list(toolsets) or []
    if args.get("workdir") is not None:
        workdir = str(args["workdir"]).strip()
        _check(workdir_problem(workdir))
        out["workdir"] = workdir or None
    if args.get("failure_deliver") is not None:
        lane = str(args["failure_deliver"]).strip()
        _check(deliver_problem(lane) if lane else None)
        out["failure_deliver"] = targets.join_tokens(targets.split_tokens(lane)) or None
    pinned = args.get("pinned")
    if isinstance(pinned, bool):
        out["pinned"] = pinned
        if pinned:
            model, provider = MAIN_ROUTE(scope)
            out["model"], out["provider"] = model, provider
        else:
            out["model"], out["provider"] = None, None
    return out


def _create(
    scope: Scope,
    backend: ports.CronBackend,
    args: Mapping[str, Any],
    *,
    run_context: object,
    session_id: str | None,
    origin: Mapping[str, Any] | None,
) -> str:
    if not str(args.get("schedule") or "").strip():
        raise _Refused("create needs a schedule")
    if args.get("paused_reason") and not args.get("paused"):
        raise _Refused("paused_reason goes with paused=true")
    spec = _common_fields(scope, backend, args, None)
    if not spec.get("no_agent") and not (spec.get("prompt") or spec.get("skills")):
        raise _Refused("create needs a prompt or at least one skill (or a script with no_agent)")
    deliver = _deliver_value(
        args.get("deliver"),
        backend=backend,
        run_context=run_context,
        session_id=session_id,
        origin=origin,
    )
    _check(deliver_problem(deliver))
    spec["deliver"] = deliver or targets.LOCAL
    if origin and run_context != "cron":
        spec["origin"] = dict(origin)
    if args.get("paused"):
        spec["paused"] = True
        if args.get("paused_reason"):
            spec["paused_reason"] = str(args["paused_reason"])
    try:
        job = backend.create(spec)
    except ValueError as exc:
        raise _Refused(str(exc)) from None
    note = "Created paused: resume it to schedule it, or run it now." if args.get("paused") else ""
    return _ok(
        job=job_view(job), message=f"Scheduled job {ports.field(job, 'id')} created. {note}".strip()
    )


def _update(
    scope: Scope,
    backend: ports.CronBackend,
    job: Any,
    args: Mapping[str, Any],
    *,
    run_context: object,
    session_id: str | None,
) -> str:
    if args.get("paused") is not None or args.get("paused_reason") is not None:
        raise _Refused("use action='pause' or 'resume' to pause or resume a job")
    changes = _common_fields(scope, backend, args, job)
    if args.get("deliver") is not None:
        deliver = _deliver_value(
            args.get("deliver"),
            backend=backend,
            run_context=run_context,
            session_id=session_id,
            origin=None,
        )
        _check(deliver_problem(deliver))
        changes["deliver"] = deliver or targets.LOCAL
    if not changes:
        raise _Refused("nothing to change: pass the fields to update")
    try:
        updated = backend.update(str(ports.field(job, "id")), changes)
    except ValueError as exc:
        raise _Refused(str(exc)) from None
    return _ok(job=job_view(updated))


def _result_text(job: Any, report: RunReport) -> str:
    name = ports.field(job, "name") or ports.field(job, "id")
    lines = [f"[Scheduled job run finished] {name} ({report.job_id}): {report.status}"]
    if report.delivery:
        lines.append(f"delivery: {report.delivery}")
    if report.error:
        lines.append(f"error: {report.error}")
    if report.output_path is not None:
        lines.append(f"output: {report.output_path}")
    if report.excerpt:
        lines.append("")
        lines.append(report.excerpt)
    return "\n".join(lines)


async def _run_tracked(
    backend: ports.CronBackend, key: tuple[str, str], job_id: str, context: str | None
) -> RunReport:
    try:
        return await backend.run_now(job_id, context=context)
    finally:
        with _lock:
            _running.discard(key)


async def _run(
    scope: Scope,
    backend: ports.CronBackend,
    job: Any,
    args: Mapping[str, Any],
    session_id: str | None,
) -> str:
    job_id = str(ports.field(job, "id"))
    context = str(args["prompt"]) if args.get("prompt") else None
    _check(prompt_problem(context))
    key = (str(scope.home), job_id)
    with _lock:
        busy = key in _running
    if busy or backend.is_running(job_id):
        raise _Refused(f"job {job_id} is already running; its result will arrive when it ends")
    with _lock:
        _running.add(key)
    sink = _sink_for(session_id)
    if sink is None:
        report = await _run_tracked(backend, key, job_id, context)
        run = {**dataclasses.asdict(report), "mode": "inline"}
        return _ok(job=job_view(backend.get(job_id) or job), run=run)
    handle = f"run-{job_id}-{secrets.token_hex(3)}"

    async def background() -> None:
        try:
            report = await _run_tracked(backend, key, job_id, context)
            text = _result_text(job, report)
        except Exception as exc:
            text = f"[Scheduled job run failed] {job_id}: {type(exc).__name__}: {exc}"
        try:
            sink(text)
        except Exception:
            log.warning("could not hand the cron run result to the session", exc_info=True)

    task = asyncio.get_running_loop().create_task(background(), name=f"cron run {job_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return _ok(
        handle=handle,
        job_id=job_id,
        note="The job is running in the background. Its result will arrive in this conversation "
        "as a new message when it ends; carry on and do not wait or check on it.",
    )


async def cronjob_manage(
    args: Mapping[str, Any],
    *,
    scope: Scope | None = None,
    session_id: str | None = None,
    run_context: object = None,
    delivery_origin: Mapping[str, Any] | None = None,
) -> str:
    """The tool handler (see the module notes)."""
    current = scope if scope is not None else scope_or_none()
    if current is None:
        return _fail("no profile is active, so there is no cron store to use")
    action = str(args.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return _fail(f"action must be one of: {', '.join(ACTIONS)}")
    problem = availability_problem(current, run_context)
    if problem:
        return _fail(problem)
    pinned_by_model = [k for k in USER_ONLY_FIELDS if args.get(k) not in (None, "")]
    if pinned_by_model:
        return _fail(
            f"{', '.join(pinned_by_model)} cannot be set from a chat: the user chooses a job's "
            "model, provider and endpoint (`indus cron edit <job> --model … --provider …`). "
            "Use pinned=true only if the user asked to keep the job on the current model."
        )
    try:
        backend = ports.backend_for(current)
        if action == "list":
            include = args.get("include_disabled")
            jobs = backend.list_jobs(include_disabled=include is not False)
            return _ok(count=len(jobs), jobs=[job_view(j) for j in jobs])
        if action == "create":
            return _create(
                current,
                backend,
                args,
                run_context=run_context,
                session_id=session_id,
                origin=delivery_origin,
            )
        ref = str(args.get("job_id") or "").strip()
        if not ref:
            return _fail(f"{action} needs job_id (an id or a name; list the jobs first)")
        job = backend.resolve(ref)
        job_id = str(ports.field(job, "id"))
        if action == "update":
            return _update(
                current, backend, job, args, run_context=run_context, session_id=session_id
            )
        if action == "pause":
            reason = args.get("paused_reason")
            return _ok(job=job_view(backend.pause(job_id, str(reason) if reason else None)))
        if action == "resume":
            return _ok(job=job_view(backend.resume(job_id)))
        if action == "remove":
            if not backend.remove(job_id):
                return _fail(f"job {job_id} could not be removed")
            _forget_incidents(current, job_id)
            return _ok(removed={"id": job_id, "name": ports.field(job, "name")})
        return await _run(current, backend, job, args, session_id)
    except _Refused as exc:
        return _fail(str(exc))
    except JobAmbiguous as exc:
        return _fail(str(exc), candidates=list(exc.candidates))
    except (JobNotFound, StoreUnavailable) as exc:
        return _fail(str(exc))


def _forget_incidents(scope: Scope, job_id: str) -> None:
    try:
        from indus.cron.incidents import IncidentLedger

        IncidentLedger.for_scope(scope).forget_job(job_id)
    except Exception:
        log.debug("could not drop the incidents of %s", job_id, exc_info=True)
