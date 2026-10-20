"""``indus doctor`` rows for the messaging gateway (P16-36).

One section, "Messaging gateway":

- the daemon: running (pid and state), stopped, stale or never started (``state.gateway_runtime``);
- each platform that is on: its library (with the install hint when missing), its credentials
  (names only, never values) and, while the daemon runs, its live state; a platform switched on in
  config without its credentials is an error, one with only some of them is "partly set up";
- the processes some platforms depend on: the signal-cli daemon (``/api/v1/check``), the SimpleX
  CLI's WebSocket port, the BlueBubbles server (``/api/v1/ping`` without the password: any HTTP
  answer means it is up) and what the WhatsApp bridge and the Photon sidecar need (Node.js, the
  script, a provider module; Photon also its two project credentials); probes are bounded and only
  touch addresses the profile configured;
- local ports: every enabled platform's webhook port (``webhook_port``, else the adapter's default),
  the Photon sidecar's loopback port (``sidecar_port``, 8789) and the API server and webhook
  receiver when on; two on one port is an error;
- Discord voice readiness (the ``discord-voice`` extra, libopus, ffmpeg) when Discord is on;
- for each platform that is on, what a stranger's direct message gets, and why.

The gateway itself is imported by name when the check runs (the CLI never imports
``indus.gateway`` at module load, D-16.246); no adapter SDK is imported.
"""

from __future__ import annotations

import contextlib
import ctypes.util
import importlib
import importlib.util
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from indus.cli.doctor import DoctorContext, Finding, Severity, register
from indus.foundation.scope import Scope

__all__ = [
    "LISTENER_DEFAULTS",
    "PROBE_TIMEOUT_S",
    "Listener",
    "check_gateway",
    "gateway_findings",
    "http_reachable",
    "listeners",
    "port_clashes",
    "tcp_reachable",
]

PROBE_TIMEOUT_S = 2.0
SETUP_FIX = "run `indus gateway setup {name}`"

# Inbound listener defaults per platform (each adapter's own default; pinned by a test against
# the adapters' constants). ``webhook_port`` under the platform's block moves one (the key named in
# ``LISTENER_SETTING`` for a platform whose port is not a webhook's).
LISTENER_DEFAULTS: Mapping[str, int] = {
    "sms": 8080,
    "whatsapp_cloud": 8090,
    "wecom_callback": 8645,
    "bluebubbles": 8645,
    "line": 8646,
    "msgraph": 8646,
    "google_chat": 8647,
    "feishu": 8765,
    "photon": 8789,
    "teams": 3978,
    "telegram": 8443,
}
LISTENER_SETTING: Mapping[str, str] = {"photon": "sidecar_port"}


@dataclass(frozen=True)
class Listener:
    platform: str
    port: int
    setting: str


# -- probes (module level so tests replace them) ---------------------------------------------------


def http_reachable(url: str) -> str | None:
    """None when something answers HTTP at ``url`` (any status), else why not."""
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S, follow_redirects=False, trust_env=False) as http:
            http.get(url)
    except httpx.HTTPError as exc:
        return type(exc).__name__
    return None


def tcp_reachable(host: str, port: int) -> str | None:
    """None when ``host:port`` accepts a TCP connection, else why not."""
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_S):
            return None
    except OSError as exc:
        return type(exc).__name__


def _where(url: str) -> str:
    """``scheme://host:port`` of ``url`` only (no path, query or user part)."""
    parsed = urlparse(url)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


# -- reading the gateway ---------------------------------------------------------------------------


def _module(name: str) -> Any:
    return importlib.import_module(f"indus.gateway.{name}")


@dataclass
class _View:
    scope: Scope
    settings: Any
    registry: Any
    credentials: Mapping[str, tuple[str, ...]]
    public: Mapping[str, tuple[str, ...]]
    secret: Callable[[str], str | None]

    @classmethod
    def load(cls, scope: Scope) -> _View:
        config = _module("config")
        return cls(
            scope=scope,
            settings=config.load_gateway_settings(scope),
            registry=_module("registry").PlatformRegistry(),
            credentials=config.BUILTIN_CREDENTIALS,
            public=config.BUILTIN_PUBLIC_ENV,
            secret=lambda name: config.read_secret(scope, name),
        )

    def label(self, name: str) -> str:
        entry = self.registry.get(name)
        return str(getattr(entry, "label", "") or name)

    def on(self) -> list[str]:
        platforms: Mapping[str, Any] = self.settings.platforms
        order = {name: n for n, name in enumerate(self.registry.names())}
        found = [name for name, p in platforms.items() if p.enabled]
        return sorted(found, key=lambda name: (order.get(name, len(order)), name))

    def extra(self, name: str) -> Mapping[str, Any]:
        found = self.settings.platforms.get(name)
        return found.extra if found is not None else {}

    def setting(self, name: str, env: str, key: str) -> str:
        value = self.secret(env) or str(self.extra(name).get(key) or "")
        return value.strip()


# -- the daemon ------------------------------------------------------------------------------------


def _daemon(scope: Scope) -> tuple[list[Finding], Mapping[str, Any]]:
    from indus.state.gateway_runtime import runtime_view

    view = runtime_view(scope.home)
    live: Mapping[str, Any] = {}
    if view.liveness == "running":
        raw = (view.data or {}).get("platforms")
        live = raw if isinstance(raw, Mapping) else {}
        state = f", {view.state}" if view.state and view.state != "running" else ""
        row = Finding("gateway.daemon", "ok", f"gateway running (pid {view.pid}{state})")
    elif view.liveness == "stopped":
        why = f": {view.reason}" if view.reason else ""
        row = Finding("gateway.daemon", "info", f"gateway stopped{why}")
    elif view.liveness == "stale":
        row = Finding(
            "gateway.daemon",
            "warn",
            f"the gateway's status file is stale ({view.reason})",
            fix="run `indus gateway status`, then `indus gateway start` if it is not running",
        )
    else:
        row = Finding("gateway.daemon", "info", "the gateway has not run for this profile yet")
    return [row], live


# -- platforms -------------------------------------------------------------------------------------


def _platform_rows(view: _View, live: Mapping[str, Any]) -> list[Finding]:
    rows: list[Finding] = []
    on = set(view.on())
    for name in view.on():
        label = view.label(name)
        missing = view.registry.missing_deps(name) if view.registry.get(name) else None
        if missing:
            rows.append(
                Finding(
                    f"gateway.{name}.library",
                    "warn",
                    f"{label}: its library is not installed",
                    fix=str(missing),
                )
            )
        names = [*view.credentials.get(name, ()), *view.public.get(name, ())]
        held = f"; set: {', '.join(names)}" if names else ""
        state = live.get(name)
        status = state.get("state") if isinstance(state, Mapping) else None
        severity: Severity = "ok"
        now = ""
        if isinstance(status, str):
            now = f"; {status} in the running gateway"
            if status in ("fatal", "retrying"):
                severity = "warn"
                code = state.get("error_code") if isinstance(state, Mapping) else None
                now += f" ({code})" if code else ""
        rows.append(
            Finding(
                f"gateway.{name}",
                severity,
                f"{label}: on{held}{now}",
                fix="run `indus gateway status` and `indus logs gateway`"
                if severity != "ok"
                else None,
            )
        )
    for name, platform in sorted(view.settings.platforms.items()):
        if name in on or platform.reason == "disabled in config":
            continue
        reason = str(platform.reason)
        if reason.startswith("missing credentials"):
            rows.append(
                Finding(
                    f"gateway.{name}",
                    "error",
                    f"{view.label(name)} is switched on in config but {reason}",
                    fix=SETUP_FIX.format(name=name),
                )
            )
            continue
        names = [*view.credentials.get(name, ()), *view.public.get(name, ())]
        present = [n for n in names if view.secret(n) is not None]
        if present and len(present) < len(names):
            absent = [n for n in names if n not in present]
            rows.append(
                Finding(
                    f"gateway.{name}",
                    "warn",
                    f"{view.label(name)} is partly set up: {', '.join(absent)} not set",
                    fix=SETUP_FIX.format(name=name),
                )
            )
    return rows


# -- sidecars and local daemons --------------------------------------------------------------------


def _reach(name: str, label: str, what: str, problem: str | None, fix: str) -> Finding:
    if problem is None:
        return Finding(f"gateway.{name}.reachable", "ok", f"{label}: {what} answers")
    return Finding(
        f"gateway.{name}.reachable", "warn", f"{label}: {what} does not answer ({problem})", fix=fix
    )


def _sidecar_rows(view: _View) -> list[Finding]:
    rows: list[Finding] = []
    on = set(view.on())
    if "signal" in on:
        url = view.setting("signal", "SIGNAL_HTTP_URL", "http_url")
        if url:
            rows.append(
                _reach(
                    "signal",
                    "Signal",
                    f"signal-cli at {_where(url)}",
                    http_reachable(url.rstrip("/") + "/api/v1/check"),
                    "start `signal-cli --account <number> daemon --http <host:port>`",
                )
            )
    if "simplex" in on:
        url = view.setting("simplex", "SIMPLEX_WS_URL", "ws_url") or "ws://127.0.0.1:5225"
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        rows.append(
            _reach(
                "simplex",
                "SimpleX",
                f"the SimpleX CLI at {_where(url)}",
                tcp_reachable(parsed.hostname or "127.0.0.1", port),
                f"start `simplex-chat -p {port}`",
            )
        )
    if "bluebubbles" in on:
        url = view.setting("bluebubbles", "BLUEBUBBLES_SERVER_URL", "server_url")
        if url:
            rows.append(
                _reach(
                    "bluebubbles",
                    "BlueBubbles",
                    f"the BlueBubbles server at {_where(url)}",
                    http_reachable(url.rstrip("/") + "/api/v1/ping"),
                    "start BlueBubbles Server on the Mac and check its address",
                )
            )
    for spec in _NODE_SIDECARS:
        if spec.name in on:
            rows.extend(_node_sidecar_rows(view, spec))
    return rows


@dataclass(frozen=True)
class _NodeSidecar:
    """A platform whose adapter runs one of our Node programs with an operator-supplied provider."""

    name: str
    label: str
    what: str  # "the bridge", "the sidecar"
    script_key: str  # the platforms.<name> key that points at the script
    default_script: str  # the package function giving the checkout's script
    provider_env: str
    provider_kind: str  # what the provider module is
    credentials: tuple[str, ...] = ()


_NODE_SIDECARS: tuple[_NodeSidecar, ...] = (
    _NodeSidecar(
        "whatsapp",
        "WhatsApp",
        "the bridge",
        "bridge_script",
        "default_bridge_script",
        "INDUS_WHATSAPP_PROVIDER",
        "WhatsApp Web provider module",
    ),
    _NodeSidecar(
        "photon",
        "Photon",
        "the sidecar",
        "sidecar_script",
        "default_sidecar_script",
        "INDUS_PHOTON_PROVIDER",
        "Photon Spectrum provider module",
        credentials=("PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET"),
    ),
)


def _node_sidecar_rows(view: _View, spec: _NodeSidecar) -> list[Finding]:
    from indus.platform.locate import which

    rows: list[Finding] = []
    name, label, what = spec.name, spec.label, spec.what
    extra = view.extra(name)
    if which("node", path=view.scope.env.get("PATH")) is None:
        rows.append(
            Finding(
                f"gateway.{name}.node",
                "error",
                f"{label}: Node.js is not on PATH ({what} runs on it)",
                fix="install Node.js 24",
            )
        )
    script = str(extra.get(spec.script_key) or "").strip()
    found = Path(script).expanduser() if script else None
    if found is None:
        with contextlib.suppress(Exception):
            found = getattr(_module(f"adapters.{name}"), spec.default_script)()
    if found is None or not found.is_file():
        rows.append(
            Finding(
                f"gateway.{name}.{_part(what)}",
                "error",
                f"{label}: {what} script was not found",
                fix=f"set platforms.{name}.{spec.script_key} to {what}'s main script",
            )
        )
    provider = str(extra.get("provider") or view.scope.env.get(spec.provider_env) or "")
    if not provider.strip():
        rows.append(
            Finding(
                f"gateway.{name}.provider",
                "error",
                f"{label}: no {spec.provider_kind} is configured",
                fix=f"set platforms.{name}.provider ({what} refuses to start without one)",
            )
        )
    absent = [n for n in spec.credentials if view.secret(n) is None]
    if absent:
        rows.append(
            Finding(
                f"gateway.{name}.credentials",
                "error",
                f"{label}: {', '.join(absent)} not set",
                fix=SETUP_FIX.format(name=name),
            )
        )
    if not rows:
        rows.append(Finding(f"gateway.{name}.{_part(what)}", "ok", f"{label}: {what} can start"))
    return rows


def _part(what: str) -> str:
    return what.rsplit(" ", 1)[-1]


# -- ports -----------------------------------------------------------------------------------------


def _port(value: object, default: int) -> int | None:
    if value is None or value == "":
        return default
    try:
        port = int(str(value).strip())
    except ValueError:
        return None
    return port if 0 <= port <= 65535 else None


def _listening(view: _View, name: str, extra: Mapping[str, Any]) -> bool:
    if name == "telegram":
        return bool(view.secret("TELEGRAM_WEBHOOK_URL") or extra.get("webhook_url"))
    if name == "feishu":
        mode = str(extra.get("connection_mode") or view.secret("FEISHU_CONNECTION_MODE") or "")
        return mode.strip().lower() == "webhook"
    if name == "google_chat":
        mode = str(extra.get("mode") or "").strip().lower()
        if mode in ("http", "pubsub"):
            return mode == "http"
        subscription = extra.get("subscription") or view.secret("GOOGLE_CHAT_SUBSCRIPTION_NAME")
        return not subscription
    return True


def listeners(view: _View) -> list[Listener]:
    """The inbound listeners the enabled platforms and HTTP surfaces will open."""
    found: list[Listener] = []
    for name in view.on():
        default = LISTENER_DEFAULTS.get(name)
        if default is None:
            continue
        extra = view.extra(name)
        if not _listening(view, name, extra):
            continue
        key = LISTENER_SETTING.get(name, "webhook_port")
        port = _port(extra.get(key), default)
        if port:
            found.append(Listener(name, port, f"platforms.{name}.{key}"))
    gateway = view.settings.gateway
    for surface in ("api_server", "webhook"):
        section = getattr(gateway, surface, None)
        if section is not None and getattr(section, "enabled", False):
            port = _port(getattr(section, "port", None), 0)
            if port:
                found.append(Listener(surface, port, f"gateway.{surface}.port"))
    return found


def port_clashes(found: list[Listener]) -> list[Finding]:
    by_port: dict[int, list[Listener]] = {}
    for item in found:
        by_port.setdefault(item.port, []).append(item)
    rows: list[Finding] = []
    for port, items in sorted(by_port.items()):
        if len(items) < 2:
            continue
        names = " and ".join(i.platform for i in items)
        rows.append(
            Finding(
                f"gateway.port.{port}",
                "error",
                f"port {port} is wanted by {names}: only the first to start can listen",
                fix=f"move one with {items[-1].setting}",
            )
        )
    if found and not rows:
        ports = ", ".join(f"{i.platform} {i.port}" for i in found)
        rows.append(Finding("gateway.ports", "ok", f"listener ports: {ports}"))
    return rows


# -- Discord voice ---------------------------------------------------------------------------------


def _voice_rows(view: _View) -> list[Finding]:
    if "discord" not in view.on():
        return []
    rows: list[Finding] = []
    missing = [m for m, mod in (("PyNaCl", "nacl"), ("davey", "davey")) if not _installed(mod)]
    if missing:
        rows.append(
            Finding(
                "gateway.discord.voice",
                "info",
                f"Discord voice channels: {' and '.join(missing)} not installed",
                fix="pip install 'indus-agent[discord-voice]'",
            )
        )
    if ctypes.util.find_library("opus") is None:
        rows.append(
            Finding(
                "gateway.discord.opus",
                "info",
                "Discord voice channels: the Opus library (libopus) is not installed",
                fix="install libopus (for example `apt install libopus0`)",
            )
        )
    if _ffmpeg(view.scope) is None:
        rows.append(
            Finding(
                "gateway.discord.ffmpeg",
                "info",
                "Discord voice channels: ffmpeg is not installed (needed to speak replies)",
                fix="install ffmpeg (for example `apt install ffmpeg`)",
            )
        )
    if not rows:
        rows.append(Finding("gateway.discord.voice", "ok", "Discord voice channels: ready"))
    return rows


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _ffmpeg(scope: Scope) -> str | None:
    from indus.tools.builtin.media.audio import find_ffmpeg

    return find_ffmpeg(scope)


# -- access ----------------------------------------------------------------------------------------


def _access_rows(view: _View) -> list[Finding]:
    rows: list[Finding] = []
    wizard = importlib.import_module("indus.cli.wizards.gateway_setup")
    port = wizard.LiveGateway(view.registry)
    for name in view.on():
        entry = view.registry.get(name)
        if entry is None or getattr(entry, "trusted_inbound", False):
            continue
        try:
            mode, why = port.access(view.scope, name)
        except Exception as exc:
            rows.append(
                Finding(
                    f"gateway.{name}.access",
                    "warn",
                    f"{view.label(name)}: access could not be read ({type(exc).__name__})",
                )
            )
            continue
        rows.append(
            Finding(
                f"gateway.{name}.access",
                "info",
                f"{view.label(name)}: a stranger's direct message gets {mode} ({why})",
            )
        )
    return rows


# -- the check -------------------------------------------------------------------------------------


def gateway_findings(scope: Scope) -> list[Finding]:
    try:
        view = _View.load(scope)
    except Exception as exc:
        return [
            Finding(
                "gateway",
                "warn",
                f"the gateway settings could not be read: {type(exc).__name__}: {exc}",
            )
        ]
    daemon, live = _daemon(scope)
    platforms = _platform_rows(view, live)
    if not view.on():
        quiet = daemon[0].severity == "info" and daemon[0].message.endswith("yet")
        rows = [] if quiet else list(daemon)
        rows.append(
            Finding(
                "gateway.platforms",
                "info",
                "no messaging platform is on (`indus gateway setup` connects one)",
            )
        )
        return rows + platforms
    return [
        *daemon,
        *platforms,
        *_sidecar_rows(view),
        *port_clashes(listeners(view)),
        *_voice_rows(view),
        *_access_rows(view),
    ]


@register("gateway", "Messaging gateway", order=130)
def check_gateway(ctx: DoctorContext) -> list[Finding]:
    return gateway_findings(ctx.scope)
