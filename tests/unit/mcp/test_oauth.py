"""MCP OAuth (P13-09): login, storage, refresh fence, background mode, device code."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.mcp import oauth, trust
from indus.mcp.config import McpServer, OAuthSettings
from indus.mcp.connect import Connection, ConnectOptions, is_permanent, open_session
from indus.mcp.oauth import (
    FENCE_SECONDS,
    IndusOAuthProvider,
    McpAuthRequired,
    McpOAuthFailed,
    OAuthStore,
    secret_prefix,
)
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.mcp import http_server
from tests.fakes.mcp.oauth_server import (
    ISSUER,
    PRM_URL,
    RESOURCE,
    FakeAuthServer,
    MemorySecrets,
    make_token,
    s256,
)

START = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SERVER = McpServer(url=RESOURCE, auth="oauth", oauth=OAuthSettings(timeout=20))


@pytest.fixture(autouse=True)
def _fresh_fences() -> None:
    oauth.reset_shared_state()


@pytest.fixture(autouse=True)
def _a_persons_login() -> Iterator[None]:
    # The logins here stand for `indus mcp login`, the one interactive context (R07.24).
    with trust.allow_interactive_auth():
        yield


def oauth_scope(
    tmp_path: Path, *, clock: Clock | None = None, env: dict[str, str] | None = None
) -> Scope:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return make_scope(home, env=EnvView(env or {"USER": "me"}), clock=clock or SystemClock())


class Browser:
    """The person: reads the printed address and approves in a browser on another thread."""

    def __init__(self, server: FakeAuthServer) -> None:
        self.server = server
        self.lines: list[str] = []
        self.opened: list[str] = []
        self.callback_status: int | None = None

    def say(self, text: str) -> None:
        self.lines.append(text)
        address = text.strip()
        if address.startswith(f"{ISSUER}/authorize"):
            threading.Thread(target=self._approve, args=(address,), daemon=True).start()

    def open_url(self, url: str) -> bool:
        self.opened.append(url)
        return False

    def _approve(self, url: str) -> None:
        target = self.server.authorize(url)
        with httpx.Client(trust_env=False, timeout=10) as client:
            self.callback_status = client.get(target).status_code


Connector = Callable[[str, McpServer, Scope, IndusOAuthProvider], Awaitable[list[str]]]


def resource_connector(fake: FakeAuthServer) -> Connector:
    async def connect(
        name: str, server: McpServer, scope: Scope, auth: IndusOAuthProvider
    ) -> list[str]:
        transport = httpx2.MockTransport(fake.handle)
        async with httpx2.AsyncClient(transport=transport, auth=auth) as client:
            response = await client.get(RESOURCE)
            assert response.status_code == 200
        return ["echo"]

    return connect


async def fetch(provider: IndusOAuthProvider, fake: FakeAuthServer) -> int:
    transport = httpx2.MockTransport(fake.handle)
    async with httpx2.AsyncClient(transport=transport, auth=provider) as client:
        return (await client.get(RESOURCE)).status_code


def seed(
    fake: FakeAuthServer,
    secrets: MemorySecrets,
    scope: Scope,
    *,
    expires_in: int,
    revoked: bool = False,
) -> OAuthStore:
    """Tokens, client and metadata as a finished login would leave them."""
    store = OAuthStore(secrets, "files", clock=scope.clock, home=scope.home)
    store.save_client(
        OAuthClientInformationFull(
            client_id="client-0",
            redirect_uris=["http://127.0.0.1:9/callback"],  # pyright: ignore[reportArgumentType]
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
    )
    store.save_metadata(OAuthMetadata.model_validate(fake._metadata()))  # pyright: ignore[reportPrivateUsage]
    issued = fake._issue()  # pyright: ignore[reportPrivateUsage]
    if revoked:
        fake.valid_access.discard(issued["access_token"])
    store.save_tokens(
        OAuthToken(
            access_token=issued["access_token"],
            refresh_token=issued["refresh_token"],
            expires_in=expires_in,
        )
    )
    return store


# -- interactive login --------------------------------------------------------------------------


def test_login_registers_runs_pkce_and_keeps_tokens_in_the_secret_store(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    ui = Browser(fake)
    scope = oauth_scope(tmp_path)
    outcome = asyncio.run(
        oauth.login(
            "files", SERVER, scope, ui=ui, secrets=secrets, connect=resource_connector(fake)
        )
    )
    assert outcome.authorized and outcome.tools == ("echo",)
    assert len(fake.registrations) == 1
    registered = fake.registrations[0]
    assert registered["token_endpoint_auth_method"] == "none"
    assert registered["redirect_uris"][0].startswith("http://127.0.0.1:")
    [(verifier, challenge)] = fake.pkce_checked
    assert s256(verifier) == challenge
    assert 43 <= len(verifier) <= 128
    assert fake.authorize_requests[0]["code_challenge_method"] == "S256"
    assert ui.callback_status == 200
    prefix = secret_prefix("files")
    access = secrets.values[f"{prefix}_ACCESS"]
    assert access in fake.valid_access
    assert f"{prefix}_REFRESH" in secrets.values
    assert f"{prefix}_CLIENT" in secrets.values and f"{prefix}_META" in secrets.values
    assert not (scope.home / "config.yaml").exists()
    shown = "\n".join(ui.lines) + caplog.text
    assert access not in shown
    assert secrets.values[f"{prefix}_REFRESH"] not in shown
    assert ui.opened == []  # no graphical browser in this env: the address is only printed


def test_remote_session_prints_the_port_forward_hint(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    ui = Browser(fake)
    scope = oauth_scope(tmp_path, env={"USER": "me", "SSH_CLIENT": "10.0.0.2 5000 22"})
    asyncio.run(
        oauth.login(
            "files", SERVER, scope, ui=ui, secrets=MemorySecrets(), connect=resource_connector(fake)
        )
    )
    port = int(fake.registrations[0]["redirect_uris"][0].rsplit(":", 1)[1].split("/")[0])
    hint = [line for line in ui.lines if "ssh -L" in line]
    assert hint and f"ssh -L {port}:127.0.0.1:{port} me@" in hint[0]
    assert ui.opened == []


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_preregistered_client_pins_its_port_and_skips_registration(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    port = free_port()
    client_secret = make_token("cs")
    secrets = MemorySecrets({"FILES_OAUTH_SECRET": client_secret})
    server = McpServer(
        url=RESOURCE,
        auth="oauth",
        oauth=OAuthSettings(
            client_id="pre-1",
            client_secret_ref="FILES_OAUTH_SECRET",
            redirect_port=port,
            timeout=20,
        ),
    )
    scope = oauth_scope(tmp_path)
    outcome = asyncio.run(
        oauth.login(
            "files",
            server,
            scope,
            ui=Browser(fake),
            secrets=secrets,
            connect=resource_connector(fake),
        )
    )
    assert outcome.authorized
    assert fake.registrations == []
    assert fake.authorize_requests[0]["client_id"] == "pre-1"
    assert fake.authorize_requests[0]["redirect_uri"] == f"http://127.0.0.1:{port}/callback"
    exchange = next(r for r in fake.token_requests if r["grant_type"] == "authorization_code")
    assert exchange["client_id"] == "pre-1" and exchange["client_secret"] == client_secret
    assert f"{secret_prefix('files')}_CLIENT" not in secrets.values  # config holds the client
    # Tokens minted for one client are not used with another.
    other = OAuthClientInformationFull(
        client_id="pre-2",
        redirect_uris=[f"http://127.0.0.1:{port}/callback"],  # pyright: ignore[reportArgumentType]
    )
    store = OAuthStore(secrets, "files", clock=scope.clock, home=scope.home, preregistered=other)
    assert store.load_tokens() is None
    assert not store.has_tokens()


def test_login_needs_auth_oauth(tmp_path: Path) -> None:
    with pytest.raises(McpOAuthFailed):
        asyncio.run(
            oauth.login(
                "files",
                McpServer(url=RESOURCE),
                oauth_scope(tmp_path),
                ui=Browser(FakeAuthServer()),
                secrets=MemorySecrets(),
            )
        )


def test_a_failed_login_leaves_the_previous_sign_in_in_place(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    scope = oauth_scope(tmp_path)
    # A sign-in from earlier (a client without a pinned port, so each login binds a fresh one).
    earlier = OAuthStore(secrets, "files", clock=scope.clock, home=scope.home)
    earlier.save_client(
        OAuthClientInformationFull(
            client_id="earlier",
            redirect_uris=["http://127.0.0.1/callback"],  # pyright: ignore[reportArgumentType]
        )
    )
    access, refresh = make_token("at"), make_token("rt")
    earlier.save_tokens(
        OAuthToken(access_token=access, token_type="Bearer", expires_in=3600, refresh_token=refresh)
    )
    before = dict(secrets.values)

    async def fails(
        name: str, server: McpServer, scope: Scope, auth: IndusOAuthProvider
    ) -> list[str]:
        # The old tokens are set aside while the login runs; a new registration is made ...
        assert not auth.store.has_tokens()
        auth.store.save_client(
            OAuthClientInformationFull(
                client_id="half-way",
                redirect_uris=["http://127.0.0.1:1/callback"],  # pyright: ignore[reportArgumentType]
            )
        )
        # ... and then the person closes the browser tab.
        raise McpOAuthFailed("no sign-in arrived", server=name)

    with pytest.raises(McpOAuthFailed):
        asyncio.run(
            oauth.login("files", SERVER, scope, ui=Browser(fake), secrets=secrets, connect=fails)
        )
    assert secrets.values == before
    store = OAuthStore(secrets, "files", clock=scope.clock, home=scope.home)
    tokens = store.load_tokens()
    assert tokens is not None and tokens.access_token == access
    assert tokens.refresh_token == refresh

    async def interrupted(
        name: str, server: McpServer, scope: Scope, auth: IndusOAuthProvider
    ) -> list[str]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            oauth.login(
                "files", SERVER, scope, ui=Browser(fake), secrets=secrets, connect=interrupted
            )
        )
    assert secrets.values == before


def test_a_login_is_refused_outside_a_persons_own_sign_in(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    with trust.background_auth(), pytest.raises(McpOAuthFailed, match="only be started"):
        asyncio.run(
            oauth.login(
                "files",
                SERVER,
                oauth_scope(tmp_path),
                ui=Browser(fake),
                secrets=secrets,
                connect=resource_connector(fake),
            )
        )
    assert fake.registrations == [] and fake.authorize_requests == [] and secrets.values == {}


def test_login_timeout_covers_the_callback_wait() -> None:
    assert oauth.login_timeout(McpServer(url=RESOURCE, auth="oauth")) == 315.0
    slow = McpServer(
        url=RESOURCE, auth="oauth", connect_timeout=600, oauth=OAuthSettings(timeout=10)
    )
    assert oauth.login_timeout(slow) == 600.0


# -- background handlers ------------------------------------------------------------------------


def test_background_without_tokens_requires_login_and_never_registers(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    scope = oauth_scope(tmp_path)
    provider = oauth.auth_for("files", SERVER, scope, secrets=MemorySecrets())
    assert provider is not None
    with pytest.raises(McpAuthRequired) as caught:
        asyncio.run(fetch(provider, fake))
    assert "indus mcp login files" in (caught.value.hint or "")
    assert is_permanent(caught.value)
    assert fake.registrations == [] and fake.authorize_requests == []
    assert fake.token_requests == []


def test_no_handler_without_auth_oauth(tmp_path: Path) -> None:
    scope = oauth_scope(tmp_path)
    assert oauth.auth_for("files", McpServer(url=RESOURCE), scope, secrets=MemorySecrets()) is None
    stdio = McpServer(command="srv", auth="oauth")
    assert oauth.auth_for("files", stdio, scope, secrets=MemorySecrets()) is None


def test_valid_stored_token_is_sent_without_refresh(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    scope = oauth_scope(tmp_path, clock=FakeClock(START))
    seed(fake, secrets, scope, expires_in=3600)
    provider = oauth.auth_for("files", SERVER, scope, secrets=secrets)
    assert provider is not None
    assert asyncio.run(fetch(provider, fake)) == 200
    assert fake.grants("refresh_token") == 0


def test_expiry_survives_a_restart(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    clock = FakeClock(START)
    scope = oauth_scope(tmp_path, clock=clock)
    seed(fake, secrets, scope, expires_in=3600)
    clock.advance(3601)
    provider = oauth.auth_for("files", SERVER, scope, secrets=secrets)
    assert provider is not None
    assert asyncio.run(fetch(provider, fake)) == 200
    assert fake.grants("refresh_token") == 1


def test_two_concurrent_refreshes_send_one_request(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    scope = oauth_scope(tmp_path, clock=FakeClock(START))
    seed(fake, secrets, scope, expires_in=0)
    first = oauth.auth_for("files", SERVER, scope, secrets=secrets)
    second = oauth.auth_for("files", SERVER, scope, secrets=secrets)
    assert first is not None and second is not None

    async def go() -> list[int]:
        fake.refresh_gate = asyncio.Event()
        one = asyncio.ensure_future(fetch(first, fake))
        two = asyncio.ensure_future(fetch(second, fake))
        for _ in range(50):
            await checkpoint()
        fake.refresh_gate.set()
        return list(await asyncio.gather(one, two))

    assert asyncio.run(go()) == [200, 200]
    assert fake.grants("refresh_token") == 1
    assert first.refreshes_sent + second.refreshes_sent == 1


def test_no_second_refresh_within_the_fence_window(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    clock = FakeClock(START)
    scope = oauth_scope(tmp_path, clock=clock)
    seed(fake, secrets, scope, expires_in=0)
    provider = oauth.auth_for("files", SERVER, scope, secrets=secrets)
    assert provider is not None

    async def go() -> None:
        assert await fetch(provider, fake) == 200
        assert fake.grants("refresh_token") == 1
        # The server drops the new token early: within the window no second refresh goes out.
        fake.valid_access.clear()
        clock.advance(10)
        with pytest.raises(McpAuthRequired):
            await fetch(provider, fake)
        assert fake.grants("refresh_token") == 1
        # Once the window has passed, one refresh is allowed again.
        clock.advance(FENCE_SECONDS)
        assert await fetch(provider, fake) == 200
        assert fake.grants("refresh_token") == 2

    asyncio.run(go())


def test_a_rejected_token_is_refreshed_once_in_the_background(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    scope = oauth_scope(tmp_path, clock=FakeClock(START))
    seed(fake, secrets, scope, expires_in=3600, revoked=True)
    provider = oauth.auth_for("files", SERVER, scope, secrets=secrets)
    assert provider is not None
    assert asyncio.run(fetch(provider, fake)) == 200
    assert fake.grants("refresh_token") == 1


def test_a_refused_refresh_parks_instead_of_retrying(tmp_path: Path) -> None:
    fake = FakeAuthServer(refuse_refresh=True)
    secrets = MemorySecrets()
    scope = oauth_scope(tmp_path, clock=FakeClock(START))
    seed(fake, secrets, scope, expires_in=0)
    provider = oauth.auth_for("files", SERVER, scope, secrets=secrets)
    assert provider is not None

    async def go() -> None:
        for _ in range(3):
            with pytest.raises(McpAuthRequired):
                await fetch(provider, fake)

    asyncio.run(go())
    assert fake.grants("refresh_token") == 1  # no retry storm
    assert fake.registrations == [] and fake.authorize_requests == []


def test_a_sign_in_stored_by_another_process_is_picked_up(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    scope = oauth_scope(tmp_path, clock=FakeClock(START))
    provider = oauth.auth_for("files", SERVER, scope, secrets=secrets)
    assert provider is not None
    with pytest.raises(McpAuthRequired):
        asyncio.run(fetch(provider, fake))
    seed(fake, secrets, scope, expires_in=3600)
    oauth.reset_shared_state()  # the login ran elsewhere: this process saw no write
    assert asyncio.run(fetch(provider, fake)) == 200


def test_logout_removes_every_entry(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets({"OTHER": "kept"})
    scope = oauth_scope(tmp_path)
    seed(fake, secrets, scope, expires_in=60)
    assert oauth.logout("files", scope, secrets=secrets) == 5  # no client secret was stored
    assert secrets.values == {"OTHER": "kept"}


def test_secret_names_are_valid_and_distinct() -> None:
    first = secret_prefix("my-server.v2")
    second = secret_prefix("my_server_v2")
    assert first != second
    for name in (first, second, secret_prefix("ü")):
        assert name.replace("_", "").isalnum() and name.isascii()


# -- device code --------------------------------------------------------------------------------


def test_device_login_polls_at_the_interval_and_stores_tokens(tmp_path: Path) -> None:
    clock = FakeClock(START)
    polls: list[float] = []
    fake = FakeAuthServer(device=True, device_script=["authorization_pending", "slow_down"])
    fake.on_device_poll = lambda: polls.append(clock.monotonic())
    secrets = MemorySecrets()
    scope = oauth_scope(tmp_path, clock=clock)
    lines: list[str] = []

    class Ui:
        def say(self, text: str) -> None:
            lines.append(text)

        def open_url(self, url: str) -> bool:
            raise AssertionError("no browser on this host")

    async def go() -> None:
        transport = httpx.MockTransport(fake.handle_v1)
        async with httpx.AsyncClient(transport=transport) as client:
            task = asyncio.ensure_future(
                oauth.device_login("files", SERVER, scope, ui=Ui(), client=client, secrets=secrets)
            )
            while not task.done():
                await checkpoint()
                if clock.pending:
                    clock.advance(1.0)
            await task

    asyncio.run(go())
    assert [b - a for a, b in zip(polls, polls[1:], strict=False)] == [5.0, 10.0]
    assert any("WDJB-MJHT" in line for line in lines)
    [registered] = fake.registrations
    assert "urn:ietf:params:oauth:grant-type:device_code" in registered["grant_types"]
    store = OAuthStore(secrets, "files", clock=clock, home=scope.home)
    tokens = store.load_tokens()
    assert tokens is not None and tokens.access_token in fake.valid_access
    assert store.load_metadata() is not None and store.load_client() is not None


def test_device_login_without_a_device_endpoint_says_so(tmp_path: Path) -> None:
    fake = FakeAuthServer(device=False)

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle_v1)) as client:
            await oauth.device_login(
                "files",
                SERVER,
                oauth_scope(tmp_path),
                ui=Browser(fake),
                client=client,
                secrets=MemorySecrets(),
            )

    with pytest.raises(McpOAuthFailed, match="device"):
        asyncio.run(go())


# -- end to end over the SDK's Streamable HTTP client --------------------------------------------


class BearerGate:
    """ASGI middleware: the MCP app answers only requests with a current bearer token."""

    def __init__(self, app: Any, fake: FakeAuthServer) -> None:
        self.app = app
        self.fake = fake

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = headers.get("authorization", "").removeprefix("Bearer ")
            if token not in self.fake.valid_access:
                challenge = f'Bearer resource_metadata="{PRM_URL}"'.encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", challenge)],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


class Split(httpx2.AsyncBaseTransport):
    """The MCP endpoint goes to the ASGI app, everything else to the fake OAuth server."""

    def __init__(self, app: Any, fake: FakeAuthServer) -> None:
        self.asgi = httpx2.ASGITransport(app=app)
        self.mock = httpx2.MockTransport(fake.handle)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/mcp":
            return await self.asgi.handle_async_request(request)
        return await self.mock.handle_async_request(request)


def split_factory(app: Any, fake: FakeAuthServer) -> Any:
    def make(endpoint: Any, *, proxy: Any, timeout: Any, auth: Any) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=Split(app, fake), headers=dict(endpoint.headers), timeout=timeout, auth=auth
        )

    return make


def public(host: str, port: int | None) -> list[str]:
    return ["93.184.216.34"]


def test_login_then_background_connect_over_streamable_http(tmp_path: Path) -> None:
    fake = FakeAuthServer()
    secrets = MemorySecrets()
    scope = oauth_scope(tmp_path)

    async def go() -> None:
        async with http_server.serving() as (raw_app, _log):
            app = BearerGate(raw_app, fake)
            factory = split_factory(app, fake)

            # Background first: nothing stored, so the connect fails for good, without a browser.
            background = oauth.auth_for("files", SERVER, scope, secrets=secrets)
            options = ConnectOptions(auth=background, http_client=factory, resolver=public)
            with pytest.raises(McpAuthRequired):
                async with open_session(
                    "files", SERVER, scope, clock=SystemClock(), options=options
                ):
                    pass
            assert fake.registrations == []

            async def connect(
                name: str, server: McpServer, scope: Scope, auth: IndusOAuthProvider
            ) -> list[str]:
                opts = ConnectOptions(auth=auth, http_client=factory, resolver=public)
                async with Connection(
                    name, server, scope, clock=SystemClock(), options=opts
                ) as live:
                    return [tool.name for tool in await live.list_tools()]

            outcome = await oauth.login(
                "files", SERVER, scope, ui=Browser(fake), secrets=secrets, connect=connect
            )
            assert outcome.authorized and "echo" in outcome.tools

            # A session's background handler now connects with the stored token.
            again = oauth.auth_for("files", SERVER, scope, secrets=secrets)
            options = ConnectOptions(auth=again, http_client=factory, resolver=public)
            async with open_session(
                "files", SERVER, scope, clock=SystemClock(), options=options
            ) as live:
                assert "echo" in {tool.name for tool in await live.list_tools()}
            assert len(fake.registrations) == 1

    asyncio.run(go())
