"""``indus mcp`` verbs (P13-12) against the fake stdio server and a local HTTP endpoint."""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from mcp.shared.auth import OAuthToken

from indus.cli import app as app_mod
from indus.cli import prompts
from indus.cli.args import CliState
from indus.cli.commands import mcp as mcp_cmd
from indus.foundation import config
from indus.foundation.scope import Scope
from indus.mcp import oauth
from indus.mcp.config import McpServer
from tests.fakes.prompts import scripted
from tests.fakes.terminal import forge_token
from tests.unit.cli.commands._helpers_d import Result
from tests.unit.cli.commands.cmdrun import write_settings

FAKE = Path(__file__).resolve().parents[3] / "fakes" / "mcp" / "stdio_server.py"
TOOLS = ["echo", "add", "getenv", "cwd", "crash"]


def run(scope: Scope, *argv: str, tty: bool = False, answers: list[Any] | None = None) -> Result:
    state = CliState(scope=scope, stdin_isatty=lambda: tty, stdout_isatty=lambda: tty)
    saved = prompts.io_factory
    if answers is not None:
        io = scripted(*answers)
        prompts.io_factory = lambda _state, _guidance: io
    try:
        with CliRunner().isolation() as (out, err, _mixed):
            code = app_mod.dispatch(list(argv), state)
    finally:
        prompts.io_factory = saved
    return Result(code, out.getvalue().decode(), err.getvalue().decode())


def settings(scope: Scope) -> dict[str, Any]:
    return config.load(scope).user_raw()


def add_fake(scope: Scope, name: str = "files", *extra: str) -> Result:
    return run(
        scope,
        "mcp",
        "add",
        name,
        "--command",
        sys.executable,
        f"--arg={FAKE}",
        "--arg=--name",
        f"--arg={name}",
        *extra,
    )


def seed(scope: Scope, text: str) -> Path:
    return write_settings(scope, text)


FAKE_ENTRY = f"""
mcp_servers:
  files:
    command: {sys.executable}
    args: [{FAKE}, --name, files]
"""


# -- add ----------------------------------------------------------------------------------------


def test_add_probes_then_saves_keeping_comments(scope: Scope) -> None:
    path = seed(scope, "# my settings, keep me\nagent:\n  max_turns: 40\n")
    result = add_fake(scope)
    assert result.code == 0, result.err
    assert "Connected to 'files': 5 tool(s)." in result.out
    text = path.read_text(encoding="utf-8")
    assert "# my settings, keep me" in text
    entry = settings(scope)["mcp_servers"]["files"]
    assert entry["command"] == sys.executable and entry["args"][-2:] == ["--name", "files"]


def test_a_saved_server_suggests_the_jobs_waiting_for_it(scope: Scope) -> None:
    """Wave 45 (D-16.981): a skill blueprint waiting for this server becomes a suggestion; a
    server saved turned off (failed probe) suggests nothing."""
    from indus.cron.suggestions import SuggestionStore

    folder = scope.home / "skills" / "web-watch"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: web-watch\ndescription: d\n"
        'blueprint: {key: pages, title: Watch the docs, schedule: "every 240m", '
        'prompt: "Check the docs pages.", integrations: [files, broken], slots: []}\n'
        "---\n\n# web-watch\n",
        encoding="utf-8",
    )
    failed = run(scope, "mcp", "add", "broken", "--command", "/no/such/server-binary")
    assert failed.code == 1 and "Suggested job:" not in failed.out
    assert SuggestionStore(scope).pending() == []
    result = add_fake(scope)
    assert result.code == 0, result.err
    assert "Suggested job: Watch the docs" in result.out
    pending = SuggestionStore(scope).pending()
    assert [(s.dedup_key, s.source) for s in pending] == [
        ("blueprint:web-watch:pages", "integration")
    ]


def test_add_refuses_a_secret_in_a_header(scope: Scope) -> None:
    token = forge_token()
    result = run(
        scope, "mcp", "add", "web", "--url", "https://mcp.example.test/mcp",
        "--header", f"Authorization=Bearer {token}", "--no-probe",
    )  # fmt: skip
    assert result.code == 2
    assert "looks like a secret" in result.err and token not in result.err + result.out
    assert "mcp_servers" not in settings(scope)


def test_header_with_a_reference_is_kept(scope: Scope) -> None:
    result = run(
        scope, "mcp", "add", "web", "--url", "https://mcp.example.test/mcp",
        "--header", "Authorization=Bearer ${WEB_TOKEN}", "--no-probe",
    )  # fmt: skip
    assert result.code == 0, result.err
    entry = settings(scope)["mcp_servers"]["web"]
    assert entry["headers"] == {"Authorization": "Bearer ${WEB_TOKEN}"}


def test_failed_probe_without_a_terminal_saves_it_turned_off(scope: Scope) -> None:
    result = run(scope, "mcp", "add", "broken", "--command", "/no/such/server-binary")
    assert result.code == 1
    assert "Could not connect to 'broken'" in result.err
    assert settings(scope)["mcp_servers"]["broken"]["enabled"] is False


def test_existing_name_needs_force_without_a_terminal(scope: Scope) -> None:
    seed(scope, FAKE_ENTRY)
    result = run(scope, "mcp", "add", "files", "--preset", "fetch", "--no-probe")
    assert result.code == 1 and "--force" in result.err
    forced = run(scope, "mcp", "add", "files", "--preset", "fetch", "--no-probe", "--force")
    assert forced.code == 0, forced.err
    assert settings(scope)["mcp_servers"]["files"] == {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
    }


def test_add_usage_errors(scope: Scope) -> None:
    both = run(scope, "mcp", "add", "x", "--command", "a", "--url", "https://h/mcp")
    assert both.code == 2
    neither = run(scope, "mcp", "add", "x")
    assert neither.code == 2
    env_on_url = run(scope, "mcp", "add", "x", "--url", "https://h/mcp", "--env", "A=b")
    assert env_on_url.code == 2


def test_oauth_server_is_saved_without_probing_and_points_at_login(scope: Scope) -> None:
    result = run(
        scope, "mcp", "add", "web", "--url", "https://mcp.example.test/mcp", "--auth", "oauth"
    )
    assert result.code == 0, result.err
    assert "indus mcp login web" in result.out
    assert settings(scope)["mcp_servers"]["web"]["auth"] == "oauth"


# -- list, enable, disable, remove ----------------------------------------------------------------


def test_list_shows_state_and_tool_selection(scope: Scope) -> None:
    seed(
        scope,
        FAKE_ENTRY
        + """
  stopped:
    command: srv
    enabled: false
  later:
    url: https://mcp.example.test/mcp
    lazy: true
    tools:
      include: [read, write]
  odd:
    url: 5
""",
    )
    result = run(scope, "mcp", "list")
    assert result.code == 0
    lines = {line.split()[0]: line for line in result.out.splitlines()[1:] if line.strip()}
    assert "configured" in lines["files"] and lines["files"].rstrip().endswith("all")
    assert "disabled" in lines["stopped"]
    assert "lazy" in lines["later"] and "2 selected" in lines["later"]
    assert "invalid" in lines["odd"]
    rows = mcp_cmd.status_rows(scope, live={"files": mcp_cmd.LiveStatus("cooling_down", 5)})
    files = next(r for r in rows if r.name == "files")
    assert files.state == "cooling down" and files.tools == "5"


def test_list_with_nothing_configured(scope: Scope) -> None:
    result = run(scope, "mcp", "list")
    assert result.code == 0 and "No MCP servers are configured." in result.out


def test_enable_and_disable(scope: Scope) -> None:
    seed(scope, FAKE_ENTRY)
    assert run(scope, "mcp", "disable", "files").code == 0
    assert settings(scope)["mcp_servers"]["files"]["enabled"] is False
    assert run(scope, "mcp", "enable", "files").code == 0
    assert settings(scope)["mcp_servers"]["files"].get("enabled", True) is True
    assert run(scope, "mcp", "enable", "ghost").code == 1


def test_remove_drops_the_entry_and_the_stored_sign_in(scope: Scope) -> None:
    path = seed(scope, "# keep\n" + FAKE_ENTRY)
    store = oauth.secrets_for(scope)
    oauth.OAuthStore(store, "files", clock=scope.clock, home=scope.home).save_tokens(
        OAuthToken(access_token="acc-" + "x" * 8, refresh_token="ref-" + "y" * 8, expires_in=60)
    )
    assert run(scope, "mcp", "remove", "files").code == 1  # no terminal and no --yes
    result = run(scope, "mcp", "remove", "files", "--yes")
    assert result.code == 0, result.err
    assert "sign-in was removed" in result.out
    assert "files" not in settings(scope).get("mcp_servers", {})
    assert "# keep" in path.read_text(encoding="utf-8")
    prefix = oauth.secret_prefix("files")
    assert store.get(f"{prefix}_ACCESS") is None and store.get(f"{prefix}_REFRESH") is None


# -- test -----------------------------------------------------------------------------------------


def test_test_lists_the_tools(scope: Scope) -> None:
    seed(scope, FAKE_ENTRY)
    result = run(scope, "mcp", "test", "files")
    assert result.code == 0, result.err
    assert "5 tool(s)" in result.out and "  echo — Return the text unchanged." in result.out
    assert run(scope, "mcp", "test", "ghost").code == 1


class _Failing(BaseHTTPRequestHandler):
    def _fail(self) -> None:
        body = b"database is down; try again later " + b"." * 400
        self.send_response(500)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._fail()

    def do_GET(self) -> None:  # noqa: N802
        self._fail()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@pytest.fixture
def failing_endpoint() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Failing)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    finally:
        server.shutdown()
        server.server_close()


def test_test_against_http_500_prints_status_url_and_body_start(
    scope: Scope, failing_endpoint: str
) -> None:
    seed(scope, f"mcp_servers:\n  web:\n    url: {failing_endpoint}\n")
    result = run(scope, "mcp", "test", "web")
    assert result.code == 1
    assert f"HTTP 500 from {failing_endpoint}: database is down" in result.err
    detail = next(line for line in result.err.splitlines() if "HTTP 500" in line)
    assert len(detail.split(": ", 1)[1]) <= mcp_cmd.BODY_CHARS + 2


# -- configure ------------------------------------------------------------------------------------


def test_configure_writes_include_for_a_partial_selection_and_removes_it_for_all(
    scope: Scope,
) -> None:
    seed(scope, FAKE_ENTRY)
    partial = run(scope, "mcp", "configure", "files", tty=True, answers=[["echo", "add"]])
    assert partial.code == 0, partial.err
    assert settings(scope)["mcp_servers"]["files"]["tools"] == {"include": ["echo", "add"]}
    everything = run(scope, "mcp", "configure", "files", tty=True, answers=[list(range(5))])
    assert everything.code == 0, everything.err
    assert "tools" not in settings(scope)["mcp_servers"]["files"]


def test_configure_pre_checks_from_the_current_filter(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(scope, FAKE_ENTRY + "    tools:\n      exclude: [crash, 'get*']\n")
    seen: list[list[int]] = []

    def checklist(io: Any, question: str, rows: Any, checked: Any = ()) -> list[int]:
        seen.append(list(checked))
        return list(checked)

    monkeypatch.setattr(prompts, "ask_checklist", checklist)
    result = run(scope, "mcp", "configure", "files", tty=True, answers=[])
    assert result.code == 0 and "Nothing changed." in result.out
    assert seen == [[TOOLS.index("echo"), TOOLS.index("add"), TOOLS.index("cwd")]]


def test_configure_without_a_terminal_needs_flags(scope: Scope) -> None:
    seed(scope, FAKE_ENTRY)
    assert run(scope, "mcp", "configure", "files").code == 2
    flagged = run(scope, "mcp", "configure", "files", "--include", "echo")
    assert flagged.code == 0
    assert settings(scope)["mcp_servers"]["files"]["tools"] == {"include": ["echo"]}
    assert run(scope, "mcp", "configure", "files", "--all").code == 0
    assert "tools" not in settings(scope)["mcp_servers"]["files"]


# -- login ----------------------------------------------------------------------------------------


def test_login_runs_the_oauth_sign_in(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(scope, "mcp_servers:\n  web:\n    url: https://mcp.example.test/mcp\n    auth: oauth\n")
    calls: list[str] = []

    async def fake_login(name: str, server: McpServer, at: Scope, *, ui: Any) -> oauth.LoginOutcome:
        calls.append(name)
        ui.say("open this address")
        return oauth.LoginOutcome(tools=("read", "write"), authorized=True)

    monkeypatch.setattr(oauth, "login", fake_login)
    result = run(scope, "mcp", "login", "web")
    assert result.code == 0, result.err
    assert calls == ["web"] and "2 tool(s)" in result.out


def test_login_refuses_a_server_without_oauth(scope: Scope) -> None:
    seed(scope, FAKE_ENTRY)
    result = run(scope, "mcp", "login", "files")
    assert result.code == 1 and "does not use OAuth" in result.err
