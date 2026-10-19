"""The one place P13-09..11 hook into a server's connect."""

from __future__ import annotations

from pathlib import Path

from indus.mcp import extras, oauth
from indus.mcp.config import McpServer, OAuthSettings, SamplingSettings
from indus.mcp.connect import ConnectOptions
from tests.fakes.mcp.oauth_server import RESOURCE, MemorySecrets
from tests.fakes.terminal import terminal_scope
from tests.unit.mcp.test_sampling import FakeBackend


def test_remote_oauth_server_gets_auth_and_callbacks(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    server = McpServer(url=RESOURCE, auth="oauth")
    base = ConnectOptions(session_kwargs={"read_timeout_seconds": 5})
    got = extras.extras_for(
        "files", server, scope, base=base, sampling=FakeBackend(), secrets=MemorySecrets()
    )
    assert isinstance(got.options.auth, oauth.IndusOAuthProvider) and got.auth is got.options.auth
    assert got.auth is not None and not got.auth.interactive
    assert set(got.options.session_kwargs) == {
        "read_timeout_seconds",
        "sampling_callback",
        "sampling_capabilities",
        "elicitation_callback",
    }
    assert got.handlers.elicitation is not None
    assert got.handlers.elicitation.requesters is got.requesters


def test_stdio_without_sampling_backend_gets_only_elicitation(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    server = McpServer(command="srv", sampling=SamplingSettings(enabled=True))
    got = extras.extras_for("files", server, scope)
    assert got.options.auth is None
    assert set(got.options.session_kwargs) == {"elicitation_callback"}


def test_unbuildable_oauth_is_reported_not_raised(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    server = McpServer(
        url=RESOURCE,
        auth="oauth",
        oauth=OAuthSettings(client_id="c", client_secret_ref="MISSING_SECRET"),
    )
    got = extras.extras_for("files", server, scope, secrets=MemorySecrets())
    assert got.auth is None and got.problem is not None and "MISSING_SECRET" in got.problem


def test_opener_is_the_guarded_one() -> None:
    assert extras.opener().__name__ == "open_checked"
