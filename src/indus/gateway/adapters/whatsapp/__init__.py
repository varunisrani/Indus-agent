"""WhatsApp through our own Node bridge (P16-02 / P16-03); see ``adapter`` and ``sidecar``.

The registry row is ``whatsapp`` in ``gateway.registry``. The platform has no credential; it turns
on with ``platforms.whatsapp.enabled: true`` once the profile is paired. The WhatsApp Web provider
is operator-supplied (``platforms.whatsapp.provider``); none ships, and without one the bridge
refuses to start (``whatsapp_provider_missing``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.whatsapp.adapter import BridgeLauncher, WhatsAppAdapter
    from indus.gateway.config import AdapterConfig

__all__ = ["create_adapter", "default_bridge_script"]

_BRIDGE_MAIN = ("apps", "whatsapp-bridge", "src", "main.ts")


def default_bridge_script() -> Path | None:
    """The bridge entry point of a source checkout (``apps/whatsapp-bridge/src/main.ts`` beside
    ``src/indus``); None for an installed package (set ``platforms.whatsapp.bridge_script``)."""
    parents = Path(__file__).resolve().parents
    if len(parents) < 6:
        return None
    checkout = parents[5]
    candidate = checkout.joinpath(*_BRIDGE_MAIN)
    if (checkout / "pyproject.toml").is_file() and candidate.is_file():
        return candidate
    return None


def create_adapter(
    config: AdapterConfig,
    *,
    launcher: BridgeLauncher | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> WhatsAppAdapter:
    """The registry factory: ``platforms.whatsapp`` settings → an adapter that owns the bridge
    (``launcher`` / ``transport`` replace the bridge process and its HTTP in tests)."""
    from indus.gateway.adapters.whatsapp.adapter import WhatsAppAdapter, WhatsAppOptions, http_probe
    from indus.gateway.adapters.whatsapp.sidecar import SidecarOwner
    from indus.platform.locate import which
    from indus.safety.child_env import child_env

    scope = config.scope
    options = WhatsAppOptions.read(config.extra, scope.env)
    script = Path(options.bridge_script).expanduser() if options.bridge_script else None

    def env_for(extra: dict[str, str]) -> dict[str, str]:
        return child_env(scope, variant="minimal", extra=extra)

    owner = SidecarOwner(
        home=scope.home,
        root=scope.root,
        profile_id=scope.profile_id,
        clock=config.clock,
        probe=http_probe(),
        node=which("node", path=scope.env.get("PATH")),
        script=script or default_bridge_script(),
        env_for=env_for,
        explicit_port=options.bridge_port,
        provider=options.provider,
        self_chat=options.self_chat,
    )
    return WhatsAppAdapter(
        clock=config.clock,
        launcher=launcher or owner,
        options=options,
        settings=config.settings,
        transport=transport,
        send_only=config.send_only,
        auth_dir=owner.auth_dir,
    )
