"""Raft for the gateway (P16-30): the wake endpoint for Raft's bridge.

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.raft:create_adapter``). ``RAFT_PROFILE`` (the Raft agent profile the
agent passes to the raft CLI; secret store first, then the environment) turns the platform on and
keys the gateway's token lock; it is not a secret. ``RAFT_CHANNEL_TOKEN`` (optional, a secret)
fixes the bridge token, else a new one is made at every start and written to the port file.
Settings: ``platforms.raft.port`` (0: an ephemeral port), ``platforms.raft.runtime_session`` (the
chat id wakes arrive on, ``default``). No extra: the listener is the P16-01 kit.
"""

from __future__ import annotations

from indus.gateway.adapters.raft.adapter import PROFILE_ENV, TOKEN_ENV, RaftAdapter
from indus.gateway.config import AdapterConfig, read_secret

__all__ = ["LOCK_ENV", "PUBLIC_ENV", "RaftAdapter", "create_adapter"]

# The value whose presence enables the platform and keys the token lock (not a secret).
PUBLIC_ENV: tuple[str, ...] = (PROFILE_ENV,)
LOCK_ENV: tuple[str, ...] = (PROFILE_ENV,)


def create_adapter(config: AdapterConfig, *, serve: bool = True) -> RaftAdapter:
    """The Raft adapter for ``config`` (``serve=False`` keeps the listener in-process for tests).
    ``ValueError`` for a port setting that is not a port number."""
    extra = config.extra
    raw_port = extra.get("port")
    if raw_port in (None, ""):
        port = 0
    elif isinstance(raw_port, bool):
        raise ValueError("platforms.raft.port must be a port number")
    else:
        port = int(str(raw_port).strip())
    if not 0 <= port <= 65535:
        raise ValueError("platforms.raft.port must be between 0 and 65535")
    profile = read_secret(config.scope, PROFILE_ENV) or str(extra.get("profile") or "")
    return RaftAdapter(
        clock=config.clock,
        home=config.scope.home,
        profile=profile,
        token=read_secret(config.scope, TOKEN_ENV),
        port=port,
        runtime_session=str(extra.get("runtime_session") or "default"),
        settings=config.settings,
        send_only=config.send_only,
        serve=serve,
    )
