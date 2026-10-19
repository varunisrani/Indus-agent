"""MCP server config: models, placeholders, the stdio env and remote endpoints (P13-01)."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config import loader, writer
from indus.foundation.scope import Scope
from indus.mcp import config as mc
from indus.mcp.config import (
    IdentityHeader,
    Interpolator,
    McpClientCertMissing,
    McpServer,
    McpUnresolvedRef,
    load_servers,
    load_settings,
    parse_server,
    remote_endpoint,
    stdio_launch,
)
from tests.fakes.terminal import forge_token, terminal_scope


def scope_with(
    tmp_path: Path,
    env: Mapping[str, str | None] | None = None,
    *,
    secrets: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> Scope:
    scope = terminal_scope(tmp_path, config, secrets=secrets)
    return scope.with_env(scope.env.with_overlay(env or {}))


# -- the model ----------------------------------------------------------------------------------


def test_defaults_follow_the_row() -> None:
    server = McpServer(command="npx")
    assert server.kind == "stdio" and not server.is_remote
    assert (server.timeout, server.connect_timeout) == (300.0, 60.0)
    assert server.lazy is False and server.enabled is True
    assert (server.idle_timeout_seconds, server.max_lifetime_seconds) == (0.0, 0.0)
    assert server.supports_parallel_tool_calls is False
    assert server.trust == "full"
    assert server.tools.include is None and server.tools.exclude is None
    assert server.tools.resources and server.tools.prompts
    assert server.sampling.max_tokens_cap == 4096 and server.sampling.max_rpm == 10
    assert server.sampling.timeout == 30.0 and server.sampling.max_tool_rounds == 5
    assert server.elicitation.enabled and server.elicitation.timeout == 300.0


def test_kind_follows_url_and_transport() -> None:
    assert McpServer(url="https://x.example/mcp").kind == "http"
    assert McpServer(url="https://x.example/sse", transport="sse").kind == "sse"


@pytest.mark.parametrize(
    ("raw", "tier", "warned"),
    [
        (None, "full", False),
        ("full", "full", False),
        ("untrusted", "untrusted", False),
        (" Untrusted ", "untrusted", False),
        ("untrsted", "untrusted", True),
        ("yes", "untrusted", True),
        (True, "untrusted", True),
    ],
)
def test_trust_tier_fails_closed(raw: object, tier: str, warned: bool) -> None:
    entry: dict[str, Any] = {"command": "srv"}
    if raw is not None:
        entry["trust"] = raw
    server, warnings, problem = parse_server("s", entry)
    assert problem is None and server is not None
    assert server.trust == tier
    assert any("trust" in w for w in warnings) is warned


def test_missing_trust_key_means_full() -> None:
    server, _, _ = parse_server("s", {"command": "srv"})
    assert server is not None and server.trust == "full"


def test_filter_lists_accept_one_string() -> None:
    server, _, _ = parse_server("s", {"command": "x", "tools": {"include": "read_*"}})
    assert server is not None and server.tools.include == ["read_*"]
    server, _, _ = parse_server("s", {"command": "x", "tools": {"include": []}})
    assert server is not None and server.tools.include == []


@pytest.mark.parametrize(
    ("entry", "needle"),
    [
        ({}, "command"),
        ({"command": "x", "timeout": -1}, "timeout"),
        ({"command": "x", "connect_timeout": 0}, "connect_timeout"),
        ({"url": "https://a/mcp", "transport": "websocket"}, "transport"),
        ({"url": "https://a/mcp", "client_cert": ["a", "b"], "client_key": "c"}, "client_cert"),
        ({"url": "https://a/mcp", "client_cert": ["a"]}, "client_cert"),
        ({"url": "https://a/mcp", "client_key": "k.pem"}, "client_key"),
        (
            {"url": "https://a/mcp", "auth": "oauth", "oauth": {"client_secret_ref": "a b"}},
            "secret",
        ),
    ],
)
def test_broken_entries_become_problems(entry: dict[str, Any], needle: str) -> None:
    server, _, problem = parse_server("s", entry)
    assert server is None and problem is not None and needle in problem


@pytest.mark.parametrize(
    ("entry", "warning"),
    [
        ({"command": "x", "idle_timeout_seconds": -5}, "idle_timeout_seconds"),
        ({"command": "x", "max_lifetime_seconds": "soon"}, "max_lifetime_seconds"),
        ({"command": "x", "tools": {"include": 5}}, "tools.include"),
    ],
)
def test_odd_optional_values_warn_and_switch_off(entry: dict[str, Any], warning: str) -> None:
    server, warnings, problem = parse_server("s", entry)
    assert problem is None and server is not None
    assert server.idle_timeout_seconds == 0 and server.max_lifetime_seconds == 0
    assert server.tools.include is None
    assert any(warning in w for w in warnings)


def test_a_secret_value_is_refused_where_a_name_belongs() -> None:
    raw = {"url": "https://a/mcp", "oauth": {"client_secret_ref": forge_token()}}
    server, _, problem = parse_server("s", raw)
    assert server is None and problem is not None and "name a stored secret" in problem
    ok, _, _ = parse_server("s", {"url": "https://a/mcp", "oauth": {"client_secret_ref": "GH_APP"}})
    assert ok is not None and ok.oauth is not None and ok.oauth.client_secret_ref == "GH_APP"


def test_both_command_and_url_uses_the_url_with_a_warning() -> None:
    server, warnings, _ = parse_server("s", {"command": "x", "url": "https://a/mcp"})
    assert server is not None and server.kind == "http"
    assert any("url is used" in w for w in warnings)


def test_unknown_keys_are_kept_and_reported() -> None:
    server, warnings, _ = parse_server("s", {"command": "x", "keepalive_interval": 5})
    assert server is not None and (server.model_extra or {})["keepalive_interval"] == 5
    assert warnings == ["keepalive_interval: unknown setting (kept)"]


def test_invalid_identity_header_is_ignored_with_a_warning() -> None:
    raw = {"url": "https://a/mcp", "identity_header": {"name": "bad name", "value": "x"}}
    server, warnings, problem = parse_server("s", raw)
    assert problem is None and server is not None and server.identity_header is None
    assert any("identity_header ignored" in w for w in warnings)


def test_load_servers_skips_only_the_broken_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = {
        "mcp_servers": {"good": {"command": "srv"}, "bad": {"args": ["x"]}, "odd": "text"},
        "mcp": {"discovery_concurrency": 0},
    }
    scope = scope_with(tmp_path, config=config)
    table = load_servers(scope)
    assert list(table.servers) == ["good"]
    assert set(table.problems) == {"bad", "odd"}
    assert load_settings(scope).discovery_concurrency == 0
    assert load_settings(scope_with(tmp_path)).discovery_concurrency == 4
    assert load_servers(scope_with(tmp_path)).servers == {}


def test_padded_values_warn_once_per_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    raw = {"url": "https://a/mcp", "headers": {"Authorization": "Bearer x\n"}}
    with caplog.at_level(logging.WARNING):
        parse_server("padded", raw)
        parse_server("padded", raw)
    hits = [r for r in caplog.records if "headers.Authorization" in r.getMessage()]
    assert len(hits) == 1
    assert "Bearer" not in hits[0].getMessage()


# -- placeholders -------------------------------------------------------------------------------


def test_secret_store_beats_the_env_view_and_env_prefix_works(tmp_path: Path) -> None:
    scope = scope_with(tmp_path, {"TOKEN": "from-env", "ONLY_ENV": "e"}, secrets={"TOKEN": "s"})
    fill = Interpolator(scope)
    assert fill.render("${TOKEN}/${env:TOKEN}/${ ONLY_ENV }") == "s/s/e"
    assert fill.unresolved == []


def test_unset_names_stay_literal_and_are_listed(tmp_path: Path) -> None:
    fill = Interpolator(scope_with(tmp_path, {"MISSING": None}))
    assert fill.render("a ${MISSING} b ${env:ALSO_MISSING}") == "a ${MISSING} b ${env:ALSO_MISSING}"
    assert fill.unresolved == ["MISSING", "ALSO_MISSING"]


def test_context_variables_are_case_sensitive(tmp_path: Path) -> None:
    work = tmp_path / "projects" / "demo"
    scope = scope_with(tmp_path, {"HOME": "/home/tester", "WORKSPACEFOLDER": "from-env"})
    fill = Interpolator(scope, work)
    assert fill.render("${workspaceFolder}") == str(work)
    assert fill.render("${workspaceFolderBasename}") == "demo"
    assert (
        fill.render("${userHome}${/}cache${pathSeparator}x")
        == f"/home/tester{os.sep}cache{os.sep}x"
    )
    assert fill.render("${WORKSPACEFOLDER}") == "from-env"
    assert fill.render("${workspacefolder}") == "${workspacefolder}"


def test_workspace_defaults_to_the_current_directory(tmp_path: Path) -> None:
    assert Interpolator(scope_with(tmp_path)).render("${workspaceFolder}") == str(Path.cwd())


# -- stdio launch -------------------------------------------------------------------------------


def test_stdio_env_is_the_safe_baseline_plus_configured_values(tmp_path: Path) -> None:
    leaked = forge_token()
    env = {
        "OPENAI_API_KEY": leaked,
        "AWS_SECRET_ACCESS_KEY": leaked,
        "PATH": "/usr/bin",
        "HOME": "/home/t",
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": "/home/t/.config",
        "SystemRoot": "C:\\Windows",
        "RANDOM_TOOL_SETTING": "1",
    }
    scope = scope_with(tmp_path, env, secrets={"GH": "store-value"})
    server = McpServer(
        command="${userHome}/bin/srv",
        args=["--root", "${workspaceFolder}", "--token=${GH}"],
        env={"GITHUB_TOKEN": "${GH}", "UNSET": "${NOPE}"},
    )
    launch = stdio_launch("files", server, scope, workspace=tmp_path)
    assert launch.command == "/home/t/bin/srv"
    assert launch.args == ("--root", str(tmp_path), "--token=store-value")
    assert launch.cwd == tmp_path
    assert launch.env["GITHUB_TOKEN"] == "store-value"
    assert launch.env["UNSET"] == "${NOPE}"
    for kept in ("PATH", "HOME", "LANG", "XDG_CONFIG_HOME", "SystemRoot"):
        assert kept in launch.env
    for dropped in ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "RANDOM_TOOL_SETTING"):
        assert dropped not in launch.env
    assert leaked not in repr(launch)


def test_configured_cwd_wins_over_the_workspace(tmp_path: Path) -> None:
    scope = scope_with(tmp_path)
    server = McpServer(command="srv", cwd="${workspaceFolder}/sub")
    assert stdio_launch("s", server, scope, workspace=tmp_path).cwd == tmp_path / "sub"
    assert stdio_launch("s", McpServer(command="srv"), scope).cwd is None


def test_identity_header_on_stdio_is_ignored_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server = McpServer(
        command="srv", identity_header=IdentityHeader(name="X-Who", value_from="profile")
    )
    with caplog.at_level(logging.WARNING):
        stdio_launch("stdio-ident", server, scope_with(tmp_path))
        stdio_launch("stdio-ident", server, scope_with(tmp_path))
    assert sum("identity_header" in r.getMessage() for r in caplog.records) == 1


# -- remote endpoint ----------------------------------------------------------------------------


def test_remote_headers_and_identity(tmp_path: Path) -> None:
    token = forge_token()
    scope = scope_with(tmp_path, secrets={"API_TOKEN": token})
    server = McpServer(
        url="https://mcp.example/${env:API_TOKEN}/mcp",
        headers={"Authorization": "Bearer ${API_TOKEN}"},
        identity_header=IdentityHeader(name="X-Indus-Profile", value_from="profile"),
    )
    endpoint = remote_endpoint("remote", server, scope)
    assert endpoint.url == f"https://mcp.example/{token}/mcp"
    assert endpoint.headers == {
        "Authorization": f"Bearer {token}",
        "X-Indus-Profile": scope.profile_id,
    }
    assert endpoint.transport == "http" and endpoint.cert is None
    assert token not in repr(endpoint)


def test_explicit_header_beats_the_identity_header(tmp_path: Path) -> None:
    server = McpServer(
        url="https://a/mcp",
        headers={"x-who": "explicit"},
        identity_header=IdentityHeader(name="X-Who", value="static-${ME}"),
    )
    endpoint = remote_endpoint("r", server, scope_with(tmp_path, {"ME": "me"}))
    assert endpoint.headers == {"x-who": "explicit"}
    plain = McpServer(
        url="https://a/mcp", identity_header=IdentityHeader(name="X-Who", value="id-${ME}")
    )
    assert remote_endpoint("r", plain, scope_with(tmp_path, {"ME": "me"})).headers == {
        "X-Who": "id-me"
    }


def test_unset_variable_in_url_or_headers_refuses(tmp_path: Path) -> None:
    server = McpServer(url="https://a/${TENANT}/mcp", headers={"Authorization": "Bearer ${TOK}"})
    with pytest.raises(McpUnresolvedRef) as caught:
        remote_endpoint("remote", server, scope_with(tmp_path, {"TENANT": None, "TOK": None}))
    assert caught.value.names == ("TENANT", "TOK")
    assert "remote" in str(caught.value) and "${TENANT}" in str(caught.value)
    assert caught.value.code == "mcp.unresolved_ref"


def test_client_certificate_shapes(tmp_path: Path) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("c")
    key.write_text("k")
    scope = scope_with(tmp_path)
    one = McpServer(url="https://a/mcp", client_cert=str(cert))
    assert remote_endpoint("r", one, scope).cert == str(cert)
    two = McpServer(url="https://a/mcp", client_cert=str(cert), client_key=str(key))
    assert remote_endpoint("r", two, scope).cert == (str(cert), str(key))
    three = McpServer(url="https://a/mcp", client_cert=[str(cert), str(key), "${PASS}"])
    scope_pass = scope_with(tmp_path, secrets={"PASS": "pw"})
    assert remote_endpoint("r", three, scope_pass).cert == (str(cert), str(key), "pw")


def test_missing_certificate_file_names_the_server(tmp_path: Path) -> None:
    server = McpServer(url="https://a/mcp", client_cert=str(tmp_path / "gone.pem"))
    with pytest.raises(McpClientCertMissing) as caught:
        remote_endpoint("corp-api", server, scope_with(tmp_path))
    assert "corp-api" in str(caught.value) and "gone.pem" in str(caught.value)
    assert caught.value.server == "corp-api"


# -- config file round trip ---------------------------------------------------------------------


def test_round_trip_through_the_config_writer_keeps_comments(scope: Scope) -> None:
    path = scope.home / "config.yaml"
    path.write_text(
        "mcp_servers:\n"
        "  # the GitHub server, token from the keychain\n"
        "  github:\n"
        "    command: npx\n"
        "    args: [-y, some-github-server]\n"
        "    env:\n"
        "      GITHUB_PERSONAL_ACCESS_TOKEN: ${GH_TOKEN_FOR_MCP}  # never a literal\n"
        "    trust: untrusted\n",
        encoding="utf-8",
    )
    view = loader.load(scope)
    data = view.as_dict()
    data["mcp_servers"]["github"]["enabled"] = False
    writer.save(scope, data)
    text = path.read_text(encoding="utf-8")
    assert "# the GitHub server, token from the keychain" in text
    assert "${GH_TOKEN_FOR_MCP}  # never a literal" in text
    assert "enabled: false" in text
    loader.reset()
    reloaded = scope.with_config(loader.load(scope))
    table = load_servers(reloaded)
    github = table.servers["github"]
    assert github.enabled is False and github.trust == "untrusted"
    assert github.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GH_TOKEN_FOR_MCP}"}


def test_module_exports_are_real() -> None:
    assert all(hasattr(mc, name) for name in mc.__all__)
