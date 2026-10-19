# pyright: strict
"""What P13-09..11 add to a server's connect, in one place for discovery and reload.

``extras_for`` gives the ``ConnectOptions`` for one server: the background OAuth handler
(``auth: oauth``), the sampling and elicitation callbacks (only for features that are on), and
the ``Requesters`` slot the call path binds around each ``tools/call``. ``opener`` is the
``lifecycle.Opener`` with the OSV malware preflight in front of every stdio spawn.

Session hosts build one ``ServerExtras`` per server and pass ``extras.options`` and
``opener()`` to ``ManagedServer``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.mcp import oauth, osv
from indus.mcp.config import McpError, McpServer
from indus.mcp.connect import ConnectOptions
from indus.mcp.sampling import Requesters, SamplingBackend, ServerHandlers, session_kwargs

__all__ = ["ServerExtras", "extras_for", "opener"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerExtras:
    options: ConnectOptions
    handlers: ServerHandlers
    requesters: Requesters
    auth: oauth.IndusOAuthProvider | None = None
    problem: str | None = None


def extras_for(
    name: str,
    server: McpServer,
    scope: Scope,
    *,
    base: ConnectOptions | None = None,
    sampling: SamplingBackend | None = None,
    secrets: oauth.SecretWriter | None = None,
    clock: Clock | None = None,
) -> ServerExtras:
    """Connect options for one server with OAuth, sampling and elicitation wired in. A server
    whose OAuth handler cannot be built still connects (and is refused by the server); the
    reason is in ``problem`` and logged once here."""
    when = clock or scope.clock
    start = base or ConnectOptions()
    auth: oauth.IndusOAuthProvider | None = None
    problem: str | None = None
    if server.auth == "oauth" and server.is_remote:
        try:
            auth = oauth.auth_for(name, server, scope, secrets=secrets, clock=when)
        except McpError as exc:
            problem = exc.message
        except Exception as exc:  # the secret store itself is unavailable
            problem = f"the secret store could not be opened ({type(exc).__name__})"
        if problem is not None:
            log.warning("MCP server %r: OAuth is not available: %s", name, problem)
    requesters = Requesters()
    handlers = session_kwargs(name, server, clock=when, backend=sampling, requesters=requesters)
    options = replace(
        start,
        auth=auth if auth is not None else start.auth,
        session_kwargs={**start.session_kwargs, **handlers.kwargs},
    )
    return ServerExtras(options, handlers, requesters, auth, problem)


def opener() -> osv.Opener:
    """``connect.open_session`` behind the OSV malware preflight."""
    return osv.guarded_opener()
