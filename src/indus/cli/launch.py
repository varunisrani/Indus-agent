"""Starts the classic REPL for one ``ReplLaunch`` (``chat`` hands over, the REPL runs, slash
commands are wired).

``run_launch`` builds what the REPL and the slash commands share, then calls
``indus.cli.repl.app.run_repl``:

- a ``SlashContext`` over ``HostPort`` (the in-process host) with the chat's store handle, the
  creation parameters of the chat's sessions (folder, toolsets) and the session row's folder for
  bang commands; ``slash_bridge.wire_slash`` binds its output, busy check and questions to the
  REPL;
- ``on_resume``: what ``/resume`` and ``/branch`` put back (``resume.resume_session``: folder unless
  ``--no-restore-cwd`` / ``--in``, auto-approve, breadcrumb), the model the session was last
  switched to (``model_override`` or its launch record) and the recap;
- the skill and bundle slash commands (``slash.skill_cmds``, P09-09/10) in
  ``ctx.extensions.skill_commands``, and the banner's skill count; the running host's plugin
  slash commands (``slash.plugin_cmds``, P13-16) in ``ctx.extensions.plugin_commands``;
- the banner, the recap of a resumed session, the status bar's first values and the seeded ``-q``
  text, sent as typed once the prompt is up (no slash or ``!`` handling);
- the P10 session features (``start_automation``): goals, loops, heartbeats, ``/btw`` and
  ``/moa`` run on the host's loop; the REPL's queued messages count as input waiting;
- voice mode (``attach_voice_mode``, P10-19): the record key, read-aloud and ``/voice``; with
  ``--voice`` it is turned on once the prompt is up;
- the status bar's source: the attached agent's route, its usage ledger (``Agent.usage``, P08-08:
  last prompt size and cost, never a cost the ledger could not price) and its compaction count.
  It reads plain counters from the REPL thread without a loop hop; a failed read only falls back
  to the bar's defaults.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from indus.cli.handoff import ReplLaunch

if TYPE_CHECKING:
    from indus.cli.repl.app import Repl
    from indus.cli.repl.status import StatusSnapshot
    from indus.cli.slash.context import SessionPort, SlashContext
    from indus.foundation.scope import Scope

__all__ = [
    "attach_voice_mode",
    "build_context",
    "make_on_resume",
    "run_launch",
    "start_automation",
    "status_from_agent",
]

log = logging.getLogger(__name__)


def _config_get(scope: Scope) -> Callable[[str, Any], Any]:
    view = scope.config

    def get(key: str, default: Any = None) -> Any:
        if view is None:
            return default
        try:
            value = view.get(key)
        except Exception:
            return default
        return default if value is None else value

    return get


def _echo(line: str) -> None:
    click.echo(line)


def build_context(launch: ReplLaunch, port: SessionPort | None = None) -> SlashContext:
    """The slash context of one chat (output goes to stdout until ``wire_slash`` binds it)."""
    from indus.cli.slash.context import Extensions, ReplState, SlashContext
    from indus.cli.slash.plugin_cmds import plugin_commands
    from indus.cli.slash.port import HostPort
    from indus.cli.slash.skill_cmds import skill_commands
    from indus.protocol.methods import SessionCreateParams
    from indus.state import sessions

    store = launch.store
    toolsets = list(launch.policy.toolsets) if launch.policy.toolsets else None
    ctx = SlashContext(
        scope=launch.scope,
        port=port if port is not None else HostPort(launch.host, launch.scope),
        session_id=launch.session_id,
        out=_echo,
        store=store,
        state=ReplState(toolsets=toolsets),
        create_params=SessionCreateParams(cwd=str(launch.cwd), toolsets=toolsets),
        extensions=Extensions(
            plugin_commands=plugin_commands(launch.scope),
            skill_commands=skill_commands(launch.scope, launch.session_id),
        ),
    )

    def session_cwd() -> str | None:
        if store is None:
            return None
        try:
            row = sessions.get(store, ctx.session_id)
        except Exception:
            return None
        found = (row or {}).get("cwd")
        return found if isinstance(found, str) and found else None

    ctx.session_cwd = session_cwd
    return ctx


def _same_route(port: SessionPort, session_id: str, policy: Any) -> bool:
    if policy is None or not policy.model:
        return True
    try:
        now = port.route(session_id)
    except Exception:
        return True
    return now.model == policy.model and (not policy.provider or now.provider == policy.provider)


def make_on_resume(
    launch: ReplLaunch, ctx: SlashContext, link: dict[str, Repl]
) -> Callable[[str], None]:
    """What runs after ``/resume`` or ``/branch`` switched the REPL to ``session_id``."""
    from indus.cli import resume
    from indus.cli.repl.banner import RecapSettings, recap
    from indus.foundation.errors import IndusError
    from indus.providers import route as routes

    get = _config_get(launch.scope)

    def on_resume(session_id: str) -> None:
        repl = link.get("repl")
        restored = resume.resume_session(
            launch.scope,
            session_id,
            store=launch.store,
            host=launch.host,
            restore=not launch.no_restore_cwd,
        )
        say: Callable[[str], None] = repl.note if repl is not None else _echo
        for line in restored.notices:
            say(line)
        policy = restored.policy
        if not _same_route(ctx.port, session_id, policy):
            try:
                target = routes.resolve(launch.scope, model=policy.model, provider=policy.provider)
                after = ctx.port.switch_route(session_id, target, keep_effort=True)
                say(f"Back on {after.model} ({after.provider}) for this session.")
                if repl is not None:
                    repl.options.model = after.model
            except Exception as exc:
                message = exc.message if isinstance(exc, IndusError) else str(exc)
                say(f"This session used {policy.model}; staying on the current model ({message}).")
        if repl is not None and restored.history and get("display.resume_display", True):
            repl.print(recap(restored.history, RecapSettings.from_config(get)))

    return on_resume


def project_skills_check(
    launch: ReplLaunch, ask: Callable[[Path, int], bool | None] | None = None
) -> int:
    """Offer once (on a terminal) to trust this repo's skills; the count left unloaded (P13-28)."""
    import sys

    from indus.learning.skills import project_trust

    def ask_terminal(root: Path, count: int) -> bool | None:
        noun = "skill" if count == 1 else "skills"
        try:
            return click.confirm(
                f"{root} has {count} project {noun} for the agent. Trust this repo and load "
                "them (they are scanned first)?",
                default=False,
            )
        except (click.Abort, EOFError):
            return None

    interactive = ask is not None or (sys.stdin.isatty() and sys.stdout.isatty())
    try:
        state = project_trust.offer_trust(
            launch.scope,
            platform="cli" if interactive else "cli-batch",
            ask=ask or ask_terminal,
            cwd=launch.cwd,
        )
    except Exception:
        log.warning("project skills could not be checked", exc_info=True)
        return 0
    return state.pending


def _banner(launch: ReplLaunch, port: SessionPort, skills: int = 0, pending: int = 0) -> Any:
    from indus import __version__
    from indus.cli.repl.banner import BannerInfo, banner

    try:
        tools = len(port.tool_names(launch.session_id))
    except Exception:
        tools = 0
    backend = str(_config_get(launch.scope)("terminal.backend", "local") or "local")
    info = BannerInfo(
        version=__version__,
        model=launch.model,
        provider=launch.provider,
        backend=backend,
        cwd=launch.cwd,
        tool_count=tools,
        skill_count=skills,
        session_title=launch.title or "",
        resumed=launch.resumed,
        project_skills_pending=pending,
    )
    return banner(info, user_home=Path.home())


def _skill_count(ctx: SlashContext) -> int:
    """Skills runnable as slash commands (bundles not counted)."""
    from indus.learning.skills.bundles import scan_bundles

    try:
        bundles = set(scan_bundles(ctx.scope.home))
    except Exception:
        bundles = set()
    return sum(1 for name in ctx.extensions.skill_commands if name not in bundles)


def status_from_agent(agent: Any) -> StatusSnapshot:
    """The status bar's numbers for one agent (see the module notes)."""
    from indus.cli.repl.status import StatusSnapshot

    usage = agent.usage.snapshot()
    state = agent.binding.state
    engine = agent.context_engine
    return StatusSnapshot(
        model=state.model,
        context_used=usage.last_prompt_tokens,
        context_max=state.context_length,
        cost=None if usage.cost_status == "unknown" else float(usage.cost_usd),
        compressions=engine.compactions if engine is not None else 0,
    )


def _status_source(launch: ReplLaunch, link: dict[str, Repl]) -> Callable[[], StatusSnapshot]:
    def source() -> StatusSnapshot:
        repl = link.get("repl")
        session_id = repl.session_id if repl is not None else launch.session_id
        return status_from_agent(launch.host.service.agent(session_id))

    return source


def start_automation(launch: ReplLaunch, ctx: SlashContext, link: dict[str, Repl]) -> None:
    """Start the host's goals, loops, heartbeats, ``/btw`` and ``/moa`` (P10-21..26) and bind
    them to the slash context. The REPL's queued messages count as input waiting, so a goal
    continuation, a loop run or a heartbeat never jumps ahead of what the person typed."""
    from indus.cli.slash.automation_cmds import HostAutomation
    from indus.service.automation import SessionAutomation

    host = launch.host
    try:
        service = host.service

        async def start() -> SessionAutomation:
            automation = SessionAutomation(service)
            automation.start()
            return automation

        automation = host.call(start())
    except Exception:
        log.warning("goals, loops and heartbeats are not available in this chat", exc_info=True)
        return

    def waiting(session_id: str) -> bool:
        repl = link.get("repl")
        return repl is not None and session_id == repl.session_id and bool(repl.pending)

    host.run(lambda: service.set_input_probe(waiting))
    ctx.automation = HostAutomation(host, automation)


def attach_voice_mode(launch: ReplLaunch, ctx: SlashContext, repl: Repl) -> None:
    """P10-19: the record key, read-aloud and ``/voice`` for this REPL; ``--voice`` turns voice
    mode on once the prompt is up (it stays off, with one message, when it cannot work here)."""
    try:
        from indus.cli.voice import attach_voice, host_services

        attach_voice(repl, ctx, host_services(launch.host, launch.scope), turn_on=launch.voice)
    except Exception:
        log.warning("voice mode is not available in this chat", exc_info=True)


def run_launch(launch: ReplLaunch, *, run: Callable[..., int] | None = None) -> int:
    """Run the classic REPL for ``launch``; returns the exit code (see the module notes)."""
    from indus.cli.repl.app import ReplOptions, run_repl
    from indus.cli.repl.banner import RecapSettings, recap
    from indus.safety.approvals import APPROVALS, session_key

    scope = launch.scope
    get = _config_get(scope)
    pending = project_skills_check(launch)
    ctx = build_context(launch)
    link: dict[str, Repl] = {}
    start_automation(launch, ctx, link)
    for line in launch.notices:
        _echo(line)
    options = ReplOptions.from_scope(
        scope,
        model=launch.model,
        title=launch.title or "",
        auto_approve=APPROVALS.auto_approve(session_key(scope, launch.session_id)),
    )
    shown_recap = None
    if launch.resumed and launch.history and get("display.resume_display", True):
        shown_recap = recap(launch.history, RecapSettings.from_config(get))

    def ready(repl: Repl) -> None:
        link["repl"] = repl
        attach_voice_mode(launch, ctx, repl)
        seed = launch.seed
        if seed is not None and seed.strip():
            repl.post(lambda: repl.send(seed))

    runner = run if run is not None else run_repl
    return runner(
        launch.host,
        launch.session_id,
        scope=scope,
        options=options,
        banner=_banner(launch, ctx.port, _skill_count(ctx), pending),
        recap=shown_recap,
        slash_ctx=ctx,
        on_resume=make_on_resume(launch, ctx, link),
        on_ready=ready,
        status_source=_status_source(launch, link),
    )
