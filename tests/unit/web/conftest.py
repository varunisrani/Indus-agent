"""Shared builders for the dashboard tests (P18-01..06)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from indus.foundation.scope import Scope
from indus.web.app import AppOptions, create_app
from indus.web.auth.basic import hash_password
from indus.web.state import DashboardState, Mode

INDEX_HTML = (
    "<!doctype html><html><head>"
    '<link rel="icon" href="/favicon.ico">'
    '<link rel="stylesheet" href="/assets/index-abc123.css">'
    '<script type="module" src="/assets/index-abc123.js"></script>'
    '<link rel="preload" href="/fonts/Sans.woff2">'
    "</head><body><div id=root></div></body></html>"
)
CSS = "@font-face{src:url(/fonts/Sans.woff2)}.a{background:url('/assets/bg.png')}"


def cheap_hash(password: str) -> str:
    """An argon2id hash with test-sized cost (the format and checks are the real ones)."""
    return hash_password(password, memory_kib=1024, iterations=1, lanes=1)


def write_bundle(directory: Path) -> Path:
    (directory / "assets").mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (directory / "assets" / "index-abc123.css").write_text(CSS, encoding="utf-8")
    (directory / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (directory / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    return directory


@dataclass
class Built:
    app: FastAPI
    client: TestClient
    state: DashboardState

    @property
    def token(self) -> str:
        return self.state.auth.token.value

    def auth(self) -> dict[str, str]:
        return {"X-Indus-Session-Token": self.token}


type Builder = Callable[..., Built]


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    return write_bundle(tmp_path / "dist")


@pytest.fixture
def build(scope: Scope, bundle_dir: Path) -> Iterator[Builder]:
    clients: list[TestClient] = []

    def make(
        *,
        mode: Mode = "dashboard",
        host: str = "127.0.0.1",
        port: int = 9119,
        config: Mapping[str, Any] | None = None,
        env: Mapping[str, str | None] | None = None,
        base_url: str | None = None,
        peer: str = "127.0.0.1",
        bundle: Path | None = None,
        browser: bool = True,
        **options: Any,
    ) -> Built:
        used = scope.with_env(scope.env.with_overlay(dict(env or {})))
        app = create_app(
            used,
            mode,
            options=AppOptions(
                host=host,
                port=port,
                bundle_dir=bundle or bundle_dir,
                config=config if config is not None else {},
                **options,
            ),
        )
        shown = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
        shown = f"[{shown}]" if ":" in shown else shown
        url = base_url or f"http://{shown}:{port}"
        parts = urlsplit(url)
        # Like a browser, every request names the page's own origin (the gated CSRF rule refuses
        # an unsafe request with no Origin and no Referer, SEC-18); ``browser=False`` sends none.
        client = TestClient(
            app,
            base_url=url,
            client=(peer, 50000),
            follow_redirects=False,
            headers={"origin": f"{parts.scheme}://{parts.netloc}"} if browser else None,
        )
        clients.append(client)
        return Built(app, client, app.state.indus)

    yield make
    for client in clients:
        client.close()


def basic_config(username: str = "admin", password: str = "correct horse") -> dict[str, Any]:
    return {
        "dashboard": {"basic_auth": {"username": username, "password_hash": cheap_hash(password)}}
    }
