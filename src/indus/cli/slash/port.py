"""``HostPort``: the ``SessionPort`` slash commands use, over the in-process host (P06-09).

Every call is a blocking call into ``InProcessHost``: plain service methods go through the host's
own wrappers; anything that touches the agent (its route binding, its tools) runs on the host's
loop thread with ``host.call`` / ``host.run``, never from the REPL thread.
"""

from __future__ import annotations

import logging

from indus.cli.slash.context import CompressReport, RouteInfo
from indus.core.route import KEEP, RouteState
from indus.foundation.scope import Scope
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.providers.route import RuntimeRoute
from indus.service.host_inprocess import InProcessHost
from indus.tools.builtin import terminal

__all__ = ["HostPort", "route_info"]

log = logging.getLogger(__name__)


def route_info(state: RouteState) -> RouteInfo:
    return RouteInfo(
        route=state.route,
        context_length=state.context_length,
        reasoning_effort=state.reasoning_effort,
        requested_effort=state.requested_effort,
    )


class HostPort:
    """``SessionPort`` over an ``InProcessHost`` (see the module notes)."""

    def __init__(self, host: InProcessHost, scope: Scope) -> None:
        self.host = host
        self.scope = scope

    def create(self, params: SessionCreateParams) -> str:
        return self.host.create(params).session_id

    def resume(self, session_id: str) -> None:
        self.host.resume(session_id)

    def close(self, session_id: str) -> None:
        self.host.close_session(session_id)

    def interrupt(self, session_id: str) -> bool:
        try:
            return self.host.interrupt(session_id).interrupted
        except ProtocolError:
            return False

    def wait_idle(self, session_id: str, timeout: float) -> bool:
        try:
            self.host.wait(session_id, timeout=timeout)
        except TimeoutError:
            return False
        except ProtocolError:
            return True  # no turn to wait for
        return True

    def steer(self, session_id: str, text: str) -> bool:
        return self.host.steer(session_id, text).queued

    def route(self, session_id: str) -> RouteInfo:
        service = self.host.service
        return self.host.run(lambda: route_info(service.agent(session_id).binding.state))

    def switch_route(
        self,
        session_id: str,
        route: RuntimeRoute,
        *,
        effort: str | None = None,
        keep_effort: bool,
    ) -> RouteInfo:
        service = self.host.service

        async def apply() -> RouteInfo:
            binding = service.agent(session_id).binding
            state = await binding.apply(route, requested_effort=KEEP if keep_effort else effort)
            return route_info(state)

        return self.host.call(apply())

    def tool_names(self, session_id: str) -> list[str]:
        service = self.host.service
        return self.host.run(lambda: service.agent(session_id).tool_names)

    def refine(self, session_id: str, focus: str | None) -> str:
        """``/refine``: start the session's background review now (P09-17); the line to show."""
        from indus.learning import learn
        from indus.learning.host import review_runner

        service = self.host.service

        async def run() -> str:
            runner = review_runner(service.agent(session_id))
            if runner is None:
                return "/refine is not available in this session (it has no background review)."
            return (await learn.refine(runner, focus=focus)).message

        return self.host.call(run())

    def stop_processes(self, session_id: str, *, all_jobs: bool) -> int:
        processes = terminal.runtime().processes
        home = str(self.scope.home)

        async def stop() -> int:
            if not all_jobs:
                return await processes.release(home, session_id)
            stopped = 0
            for record in processes.list(home, session_id):
                if not record.exited:
                    await processes.kill(record, source="/stop")
                    stopped += 1
            return stopped

        return self.host.call(stop())

    def compress(
        self, session_id: str, focus: str | None, *, keep_last: int | None, preview: bool
    ) -> CompressReport:
        service = self.host.service

        async def run() -> CompressReport:
            outcome = await service.compress(
                session_id, focus, keep_last=keep_last, preview=preview
            )
            return CompressReport(
                status=outcome.status,
                session_id=service.current_id(session_id),
                reason=outcome.reason,
                before=outcome.before,
                after=outcome.after,
                condensed=outcome.condensed,
                mode=outcome.mode,
            )

        return self.host.call(run())
