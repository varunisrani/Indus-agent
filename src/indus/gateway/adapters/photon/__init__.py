"""iMessage through Photon (P16-25): our own Node sidecar holds the Spectrum link; see ``adapter``
and ``sidecar``.

**Shipped disabled.** No Spectrum client ships (its licences and offline availability are not
cleared; docs/notes/P16/photon.md): the provider is operator-supplied through the sidecar's
``SpectrumLink`` interface (``platforms.photon.provider`` or ``INDUS_PHOTON_PROVIDER``, a module
that exports ``createSpectrumLink``). The platform is not turned on by its credentials alone: it
needs ``platforms.photon.enabled: true``, ``PHOTON_PROJECT_ID`` and ``PHOTON_PROJECT_SECRET``
(secret store first, then the environment; vendor names, D-16.22) and a provider, else the connect
fails for good (``photon_provider_missing`` / ``photon_credentials_missing``).

Settings (``platforms.photon.*``): ``provider``, ``sidecar_port`` (8789), ``sidecar_script`` (the
checkout's ``apps/photon-sidecar/src/main.ts`` by default), ``require_mention`` (off),
``mention_patterns``, ``read_receipts`` (on), ``reactions`` (off), ``markdown`` (on).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.photon.adapter import PhotonAdapter, SidecarLauncher
    from indus.gateway.adapters.photon.sidecar import DependencyHealer
    from indus.gateway.config import AdapterConfig

__all__ = [
    "CREDENTIAL_ENV",
    "LOCK_ENV",
    "PLATFORM_HINT",
    "create_adapter",
    "default_sidecar_script",
]

# Read by the adapter (not a ``BUILTIN_CREDENTIALS`` row while the platform ships disabled).
CREDENTIAL_ENV: tuple[str, ...] = ("PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET")
# One Photon project answers in one gateway; a new project secret is the same project.
LOCK_ENV: tuple[str, ...] = ("PHOTON_PROJECT_ID",)
PLATFORM_HINT = (
    "You are texting through iMessage. Keep replies short and conversational. Light Markdown "
    "(bold, lists) renders, but a message holding a link is sent as plain text, so avoid tables, "
    "headings and code fences, and write links as bare URLs. A message that is only a URL becomes "
    "a link preview."
)
_SIDECAR_MAIN = ("apps", "photon-sidecar", "src", "main.ts")


def default_sidecar_script() -> Path | None:
    """The sidecar entry point of a source checkout (``apps/photon-sidecar/src/main.ts`` beside
    ``src/indus``); None for an installed package (set ``platforms.photon.sidecar_script``)."""
    parents = Path(__file__).resolve().parents
    if len(parents) < 6:
        return None
    checkout = parents[5]
    candidate = checkout.joinpath(*_SIDECAR_MAIN)
    if (checkout / "pyproject.toml").is_file() and candidate.is_file():
        return candidate
    return None


def create_adapter(
    config: AdapterConfig,
    *,
    launcher: SidecarLauncher | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    healer: DependencyHealer | None = None,
) -> PhotonAdapter:
    """The registry factory: ``platforms.photon`` settings → an adapter that owns the sidecar
    (``launcher`` / ``transport`` / ``healer`` replace the process, its HTTP and ``npm ci`` in
    tests)."""
    from indus.gateway.adapters.photon.adapter import PhotonAdapter, PhotonOptions
    from indus.gateway.adapters.photon.sidecar import (
        DEFAULT_PORT,
        DependencyHealer,
        PhotonSidecar,
        http_probe,
    )
    from indus.gateway.config import read_secret
    from indus.platform.locate import which
    from indus.safety.child_env import child_env

    scope = config.scope
    options = PhotonOptions.read(config.extra, scope.env)
    project_id, project_secret = (
        (config.settings.credentials.get(name) or read_secret(scope, name) or "").strip()
        for name in CREDENTIAL_ENV
    )
    script = Path(options.sidecar_script).expanduser() if options.sidecar_script else None

    def env_for(extra: dict[str, str]) -> dict[str, str]:
        return child_env(scope, variant="minimal", extra=extra)

    path = scope.env.get("PATH")
    owner = PhotonSidecar(
        home=scope.home,
        profile_id=scope.profile_id,
        clock=config.clock,
        probe=http_probe(),
        node=which("node", path=path),
        script=script or default_sidecar_script(),
        env_for=env_for,
        port=options.sidecar_port or DEFAULT_PORT,
        provider=options.provider,
        project_id=project_id,
        project_secret=project_secret,
        healer=healer or DependencyHealer(npm=which("npm", path=path), env_for=env_for),
    )
    return PhotonAdapter(
        clock=config.clock,
        launcher=launcher or owner,
        options=options,
        settings=config.settings,
        transport=transport,
        send_only=config.send_only,
        secrets=(project_id, project_secret),
    )
