"""Serving the built SPA, and building it on first launch (P18-01, R09.5).

``mount_spa(app, state)`` adds the routes that must come after every API router:

- ``/api`` and ``/api/...`` with any method → JSON 404 naming the path (a missing endpoint is never
  answered with HTML, which would break JSON clients);
- serve mode (headless): every other path → JSON 404 as well; no HTML, no assets, even when a
  bundle is on disk;
- dashboard mode: ``/assets/<file>`` from the bundle's ``assets`` dir, marked immutable (the names
  carry content hashes; CSS is rewritten under a proxy prefix and varies on it); any other GET →
  the file when it resolves inside the bundle, else ``index.html`` (client-side routes) with the
  bootstrap script injected and ``Cache-Control: no-store`` so a reload always sees the current
  session token.

Paths with ``..`` segments, backslashes or NUL (also percent-encoded) are refused with a JSON 404,
and a file is served only when its resolved path stays inside the bundle directory. A missing
bundle (or ``index.html``) answers a JSON 404 naming the build command on every request, so a build
that appears while the process runs is picked up without a restart.

Bootstrap: one inline ``<script>`` before ``</head>`` sets ``__INDUS_SESSION_TOKEN__`` (loopback
mode only), ``__INDUS_BASE_PATH__``, ``__INDUS_AUTH_REQUIRED__``,
``__INDUS_DASHBOARD_EMBEDDED_CHAT__``, ``__INDUS_DASHBOARD_CONSOLE__`` (P24-36),
``__INDUS_INITIAL_PROFILE__`` and
``__INDUS_DASHBOARD_PROFILE__``. Values are JSON with ``<``, ``>``,
``&`` and the two JavaScript line separators escaped, so no value can close the script. The active
theme's first-paint CSS goes in just before it.

Framing: every answer from the bundle (index and files) carries ``FRAME_GUARD`` — no page may frame
the dashboard (its loopback index holds the session token, so a framing site could otherwise drive
it with a person's clicks), and no file is content-sniffed into another type.

``BuildPlan``: when the bundle is missing in a source checkout (``apps/web/package.json``) and
``pnpm`` is on PATH, the dashboard builds once before serving: ``pnpm --filter @indus/web build``
with the Node heap capped at half the cgroup memory limit (at most 4 GiB, at least 512 MiB) and
``UV_THREADPOOL_SIZE`` at half the cores.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from indus.web.auth.gate import request_prefix
from indus.web.bundle import runtime_bundle_dir
from indus.web.prefix import rewrite_css, rewrite_index_html
from indus.web.state import DashboardState

__all__ = [
    "BUILD_COMMAND",
    "FRAME_GUARD",
    "IMMUTABLE",
    "NO_STORE",
    "BuildPlan",
    "SpaBundle",
    "bootstrap_script",
    "build_plan",
    "default_bundle_dir",
    "mount_spa",
    "run_build",
    "theme_css",
]

log = logging.getLogger(__name__)

BUILD_COMMAND: Final = "pnpm --filter @indus/web build"
IMMUTABLE: Final = "public, max-age=31536000, immutable"
NO_STORE: Final = "no-store"
FRAME_GUARD: Final[Mapping[str, str]] = {
    "content-security-policy": "frame-ancestors 'none'",
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
}
_ALL_METHODS: Final = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_THEMES: Final[dict[str, tuple[str, str, str]]] = {
    "light": ("light", "#f7f7f5", "#1d1d1b"),
    "dark": ("dark", "#141413", "#ececea"),
}
MIN_HEAP_MIB: Final = 512
MAX_HEAP_MIB: Final = 4096


def default_bundle_dir() -> Path:
    """The package's runtime tree, ``indus/web/static`` (``web.bundle.locate`` chooses)."""
    return runtime_bundle_dir()


@dataclass(frozen=True, slots=True)
class SpaBundle:
    """The built SPA's directory (it may not exist yet)."""

    directory: Path

    @property
    def index(self) -> Path:
        return self.directory / "index.html"

    def inside(self, relative: str) -> Path | None:
        """The file at ``relative`` when it is safely inside the bundle, else None."""
        if not relative or "\\" in relative or "\x00" in relative:
            return None
        parts = relative.split("/")
        if any(part in ("..", ".") for part in parts):
            return None
        try:
            root = self.directory.resolve(strict=True)
            target = (self.directory / relative).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not target.is_relative_to(root) or not target.is_file():
            return None
        return target


def _js(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False)
    for raw, escaped in (
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("&", "\\u0026"),
        (" ", "\\u2028"),
        (" ", "\\u2029"),
    ):
        text = text.replace(raw, escaped)
    return text


def bootstrap_script(state: DashboardState, prefix: str) -> str:
    """The inline script with the runtime globals (see the module notes)."""
    lines: list[str] = []
    if not state.gated:
        lines.append(f"window.__INDUS_SESSION_TOKEN__={_js(state.auth.token.value)};")
    lines.extend(
        [
            f"window.__INDUS_BASE_PATH__={_js(prefix)};",
            f"window.__INDUS_AUTH_REQUIRED__={_js(state.gated)};",
            f"window.__INDUS_DASHBOARD_EMBEDDED_CHAT__={_js(state.embedded_chat)};",
            f"window.__INDUS_DASHBOARD_CONSOLE__={_js(state.console_enabled)};",  # P24-36
            f"window.__INDUS_INITIAL_PROFILE__={_js(state.initial_profile)};",
            f"window.__INDUS_DASHBOARD_PROFILE__={_js(state.profile_name)};",
        ]
    )
    return "<script>" + "".join(lines) + "</script>"


def theme_css(theme: str) -> str:
    """First-paint CSS for our light/dark pair; ``system`` follows the OS."""
    found = _THEMES.get(theme)
    if found is None:
        light, dark = _THEMES["light"], _THEMES["dark"]
        return (
            '<style id="indus-theme-bootstrap">:root{color-scheme:light dark}'
            f"html,body{{background:{light[1]};color:{light[2]}}}"
            "@media (prefers-color-scheme: dark){"
            f"html,body{{background:{dark[1]};color:{dark[2]}}}}}</style>"
        )
    scheme, background, foreground = found
    return (
        f'<style id="indus-theme-bootstrap">:root{{color-scheme:{scheme}}}'
        f"html,body{{background:{background};color:{foreground}}}</style>"
    )


def _not_built(where: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": "not_built",
            "detail": f"The dashboard has not been built ({where} is missing).",
            "build": BUILD_COMMAND,
        },
        status_code=404,
        headers={"cache-control": NO_STORE},
    )


def _not_found(path: str) -> JSONResponse:
    return JSONResponse(
        {"error": "not_found", "detail": f"No route answers {path}."},
        status_code=404,
        headers={"cache-control": NO_STORE},
    )


def _index(state: DashboardState, bundle: SpaBundle, request: Request) -> Response:
    try:
        text = bundle.index.read_text(encoding="utf-8-sig")
    except OSError:
        return _not_built("index.html")
    prefix = request_prefix(request.scope)
    text = rewrite_index_html(text, prefix)
    head = theme_css(state.theme) + bootstrap_script(state, prefix)
    text = text.replace("</head>", head + "</head>", 1) if "</head>" in text else head + text
    return HTMLResponse(text, headers={"cache-control": NO_STORE, **FRAME_GUARD})


def mount_spa(app: FastAPI, state: DashboardState) -> None:
    """Add the catch-all routes (call after every API router is included)."""

    @app.api_route("/api", methods=_ALL_METHODS, include_in_schema=False)
    @app.api_route("/api/{rest:path}", methods=_ALL_METHODS, include_in_schema=False)
    async def unknown_api(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return _not_found(request.url.path)

    if state.mode == "serve" or not isinstance(state.bundle, SpaBundle):

        @app.api_route("/{rest:path}", methods=_ALL_METHODS, include_in_schema=False)
        async def headless(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
            return JSONResponse(
                {
                    "error": "not_found",
                    "detail": "This is the headless backend (indus serve): no web pages here.",
                },
                status_code=404,
                headers={"cache-control": NO_STORE},
            )

        return

    bundle: SpaBundle = state.bundle

    @app.get("/assets/{name:path}", include_in_schema=False)
    async def asset(name: str, request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not bundle.directory.is_dir():
            return _not_built("the bundle directory")
        found = bundle.inside(f"assets/{name}")
        if found is None:
            return _not_found(request.url.path)
        if found.suffix == ".css":
            prefix = request_prefix(request.scope)
            css = rewrite_css(found.read_text(encoding="utf-8-sig"), prefix)
            return Response(
                css,
                media_type="text/css",
                headers={"cache-control": IMMUTABLE, "vary": "X-Forwarded-Prefix", **FRAME_GUARD},
            )
        return FileResponse(found, headers={"cache-control": IMMUTABLE, **FRAME_GUARD})

    @app.get("/{rest:path}", include_in_schema=False)
    async def page(rest: str, request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if rest and ("\\" in rest or "\x00" in rest or ".." in rest.split("/")):
            return _not_found(request.url.path)
        if not bundle.directory.is_dir():
            return _not_built("the bundle directory")
        if rest and rest != "index.html":
            found = bundle.inside(rest)
            if found is not None:
                return FileResponse(found, headers={"cache-control": "no-cache", **FRAME_GUARD})
        return _index(state, bundle, request)

    @app.api_route(
        "/{rest:path}",
        methods=["POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def no_page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return _not_found(request.url.path)


# -- building on first launch ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildPlan:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    heap_mib: int
    threads: int


def cgroup_memory_bytes(root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """The cgroup memory limit (v2 ``memory.max``, else v1), or None when unlimited/unknown."""
    for candidate in (root / "memory.max", root / "memory" / "memory.limit_in_bytes"):
        try:
            text = candidate.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if text.isdigit():
            value = int(text)
            if 0 < value < 1 << 60:
                return value
        return None
    return None


def heap_mib(limit_bytes: int | None) -> int:
    if limit_bytes is None:
        return MAX_HEAP_MIB
    half = limit_bytes // 2 // (1024 * 1024)
    return max(MIN_HEAP_MIB, min(MAX_HEAP_MIB, half))


def build_plan(
    repo_root: Path,
    env: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    memory_limit: Callable[[], int | None] = cgroup_memory_bytes,
    cpu_count: Callable[[], int | None] = os.cpu_count,
) -> BuildPlan | None:
    """How to build the SPA here, or None (not a source checkout, or no ``pnpm``)."""
    if not (repo_root / "apps" / "web" / "package.json").is_file():
        return None
    pnpm = which("pnpm")
    if not pnpm:
        return None
    heap = heap_mib(memory_limit())
    threads = max(1, (cpu_count() or 2) // 2)
    child_env = dict(env)
    options = child_env.get("NODE_OPTIONS", "")
    child_env["NODE_OPTIONS"] = f"{options} --max-old-space-size={heap}".strip()
    child_env["UV_THREADPOOL_SIZE"] = str(threads)
    return BuildPlan(
        argv=(pnpm, "--filter", "@indus/web", "build"),
        cwd=repo_root,
        env=child_env,
        heap_mib=heap,
        threads=threads,
    )


def run_build(
    plan: BuildPlan,
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = 900.0,
) -> bool:
    """Run the build; True when it exited 0."""
    log.info("building the dashboard (heap %d MiB, %d threads)", plan.heap_mib, plan.threads)
    argv: Sequence[str] = list(plan.argv)
    try:
        done = runner(argv, cwd=str(plan.cwd), env=plan.env, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("the dashboard build could not run: %s", exc)
        return False
    return getattr(done, "returncode", 1) == 0
