"""The session switches of ``config.set`` on the daemon (W50-a, D-16.549): ``yolo``, ``fast``,
``reasoning`` and ``model`` for a session an attached client named.

The values are checked by ``service.config_writes`` (the bridge's checks and messages); what
differs is where the change goes, so every viewer of the shared session sees one state:

- ``yolo`` → ``DaemonSessionHost.set_auto_approve`` (the session row first, then the switch; an
  unattended turn's own switch no longer undoes it);
- ``fast`` → the session's fast mode;
- ``reasoning`` → the session's route binding, under the session's mutation gate (never while a
  claimed turn is being prepared); the answer is the effort in use (``none`` when the model has
  none), as in ``--local``;
- ``model`` → ``session.mutate``'s ``model`` operation (the gate, the row, ``deferred`` while a
  turn runs). A model in ``tui.confirm_models``, or one whose window cannot hold the context in
  use, answers ``CONFIRM_REQUIRED`` until ``confirm``; a model that cannot be used answers
  ``PROVIDER_UNAVAILABLE``.

A switch that applied puts a fresh ``session.info`` into the session's events (a model change
does so through the mutation), as the bridge sends its viewer one.
"""

from __future__ import annotations

import secrets

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.attach import SessionMutateParams
from indus.protocol.bridge import ConfigApplied, ConfigKey, ConfigSetParams, ConfigSetResult
from indus.protocol.errors import CONFIRM_REQUIRED, PROVIDER_UNAVAILABLE
from indus.protocol.methods import ProtocolError
from indus.service import config_writes
from indus.service.daemon_host import DaemonSessionHost

__all__ = ["set_switch"]


async def set_switch(
    host: DaemonSessionHost, key: str, params: ConfigSetParams, *, scope: Scope
) -> ConfigSetResult:
    """Apply one session switch to the hosted session ``key`` (the caller checked the
    connection attached it)."""
    name = params.key
    if name is ConfigKey.MODEL:
        return await _model(host, key, params, scope)
    sid = host.service.current_id(key)
    host.service.agent(sid)  # SESSION_NOT_FOUND before the value is looked at, as the bridge
    if name is ConfigKey.YOLO:
        enabled = config_writes.yolo_value(params.value)
        await host.set_auto_approve(key, enabled)
        value: str | bool = enabled
    elif name is ConfigKey.FAST:
        value = config_writes.fast_value(params.value)
        config_writes.switch_fast(scope, sid, value)
    else:  # reasoning
        effort = config_writes.reasoning_value(params.value)
        async with host.mutations.gate(key):
            binding = host.service.agent(host.service.current_id(key)).binding
            try:
                await binding.apply(binding.state.route, requested_effort=effort)
            except Exception as exc:
                raise config_writes.bad_value(f"reasoning not changed: {exc}") from exc
            value = binding.state.reasoning_effort or "none"
    host.announce_info(key)
    return ConfigSetResult(key=name, value=value, applied=ConfigApplied.NOW, saved=False)


async def _model(
    host: DaemonSessionHost, key: str, params: ConfigSetParams, scope: Scope
) -> ConfigSetResult:
    sid = host.service.current_id(key)
    host.service.agent(sid)
    wanted = config_writes.model_value(params.value)
    await run_blocking(
        config_writes.check_confirm, scope, wanted, params.confirm, label="settings read"
    )
    provider, _, model = wanted.partition(":") if ":" in wanted else ("", "", wanted)
    payload: dict[str, str | bool] = {"model": model.strip(), "confirm": params.confirm}
    if provider.strip():
        payload["provider"] = provider.strip()
    change = SessionMutateParams.model_validate(
        {
            "session_id": sid,
            "operation": "model",
            "payload": payload,
            "request_id": f"config-{secrets.token_hex(8)}",
        }
    )
    done = await host.mutations.mutate(key, change)
    if done.status == "confirm":
        raise ProtocolError(
            CONFIRM_REQUIRED, done.message or f"confirm the switch to {wanted}", path="confirm"
        )
    if done.status == "refused":
        raise ProtocolError(
            PROVIDER_UNAVAILABLE, done.message or done.reason or f"cannot switch to {wanted}"
        )
    applied = ConfigApplied.DEFERRED if done.status == "deferred" else ConfigApplied.NOW
    return ConfigSetResult(key=ConfigKey.MODEL, value=wanted, applied=applied, saved=False)
