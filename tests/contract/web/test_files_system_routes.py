"""P18-16: the Files and System REST routes, and the background actions they start."""

from __future__ import annotations

import logging
import os
import secrets
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import SecretStore
from indus.plugins import shell_hooks as sh
from indus.web.app import AppOptions
from indus.web.auth.basic import hash_password
from indus.web.auth.download_ticket import DOWNLOAD_TICKET_TTL_S, DownloadTickets
from indus.web.routes import files, system
from tests.contract.web.rest_app import (
    RestApp,
    build_rest_app,
    pause,
    route_table,
    unscoped_writes,
)

WRITE = {"profile": "work"}

STREAMING = """
import pathlib, sys, time
print("line one", flush=True)
go = pathlib.Path(sys.argv[-1])
for _ in range(400):
    if go.exists():
        break
    time.sleep(0.025)
print("line two", flush=True)
"""


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "managed"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_text("hello files\n", encoding="utf-8")
    (root / "docs" / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (root / ".env").write_text("NOT_SHOWN=1\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("keep out\n", encoding="utf-8")
    (root / "link-out").symlink_to(outside, target_is_directory=True)
    return root


@pytest.fixture
def rest(scope: Scope, tree: Path, tmp_path: Path) -> Iterator[RestApp]:
    config_loader.reset()
    env_scope = scope.with_env(scope.env.with_overlay({files.FILES_ROOT_ENV: str(tree)}))
    go = tmp_path / "go"

    def command(args: list[str]) -> list[str]:
        return [sys.executable, "-c", STREAMING, *args, str(go)]

    built = build_rest_app(env_scope, extras={"actions.command": command})
    yield built
    built.close()
    config_loader.reset()


def _wait(rest: RestApp, name: str, until: str = "done", limit: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + limit
    while True:
        got = rest.client.get(f"/api/actions/{name}/status", params=WRITE).json()
        if got["state"] != "running" or time.monotonic() > deadline:
            return got
        pause(0.05)


# -- route lists ----------------------------------------------------------------------------------


def test_route_lists_are_pinned(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/files",)) == set(files.ROUTE_LIST)
    prefixes = (
        "/api/system",
        "/api/memory",
        "/api/credentials",
        "/api/ops",
        "/api/actions",
        "/api/backups",
    )
    assert route_table(rest.app, prefixes) == set(system.ROUTE_LIST)
    assert unscoped_writes(rest.app, prefixes) == []
    assert sorted(unscoped_writes(rest.app, ("/api/files",))) == [
        "DELETE /api/files",
        "POST /api/files/download-ticket",
        "POST /api/files/mkdir",
        "POST /api/files/upload",
    ]


# -- files: the root ------------------------------------------------------------------------------


def test_listing_a_locked_root_hides_secrets_and_links_out(rest: RestApp, tree: Path) -> None:
    got = rest.client.get("/api/files")
    assert got.status_code == 200
    body = got.json()
    assert body["path"] == str(tree.resolve()) and body["locked_root"] == str(tree.resolve())
    assert body["parent"] is None
    assert [e["name"] for e in body["entries"]] == ["docs"]
    inner = rest.client.get("/api/files", params={"path": "docs"}).json()
    assert [e["name"] for e in inner["entries"]] == ["a.txt", "blob.bin"]
    assert inner["parent"] == str(tree.resolve())


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("../", 400, "path_traversal"),
        ("docs/../../outside", 400, "path_traversal"),
        ("..", 400, "path_traversal"),
        ("link-out", 403, "outside_root"),
        ("link-out/private.txt", 403, "outside_root"),
        (".env", 403, "sensitive_path"),
    ],
)
def test_forced_root_refuses_escapes(rest: RestApp, path: str, status: int, code: str) -> None:
    for route in ("/api/files", "/api/files/read"):
        answer = rest.client.get(route, params={"path": path})
        assert answer.status_code == status, (route, answer.text)
        assert _code(answer) == code
    minted = rest.client.post("/api/files/download-ticket", json={"path": path})
    assert minted.status_code == status, minted.text
    assert _code(minted) == code


def test_an_absolute_path_outside_the_root_is_refused(rest: RestApp, tmp_path: Path) -> None:
    answer = rest.client.get("/api/files/read", params={"path": str(tmp_path / "outside")})
    assert answer.status_code == 403 and _code(answer) == "outside_root"
    write = rest.client.post("/api/files/mkdir", json={"path": str(tmp_path / "evil")})
    assert write.status_code == 403
    assert not (tmp_path / "evil").exists()


def test_read_text_and_binary(rest: RestApp) -> None:
    text = rest.client.get("/api/files/read", params={"path": "docs/a.txt"}).json()
    assert text["text"] == "hello files\n" and text["mime_type"] == "text/plain"
    assert text["truncated"] is False and text["size"] == 12
    blob = rest.client.get("/api/files/read", params={"path": "docs/blob.bin"}).json()
    assert blob["text"] is None


def test_write_round_trip(rest: RestApp, tree: Path) -> None:
    made = rest.client.post("/api/files/mkdir", json={"path": "new/deeper"})
    assert made.status_code == 200 and (tree / "new" / "deeper").is_dir()
    up = rest.client.post(
        "/api/files/upload", data={"path": "new/up.txt"}, files={"file": ("up.txt", b"payload")}
    )
    assert up.status_code == 200 and (tree / "new" / "up.txt").read_bytes() == b"payload"
    again = rest.client.post(
        "/api/files/upload", data={"path": "new/up.txt"}, files={"file": ("up.txt", b"other")}
    )
    assert again.status_code == 409 and _code(again) == "file_exists"
    over = rest.client.post(
        "/api/files/upload",
        data={"path": "new/up.txt", "overwrite": "true"},
        files={"file": ("up.txt", b"other")},
    )
    assert over.status_code == 200 and (tree / "new" / "up.txt").read_bytes() == b"other"
    not_empty = rest.client.delete("/api/files", params={"path": "new"})
    assert not_empty.status_code == 409 and _code(not_empty) == "not_empty"
    gone = rest.client.delete("/api/files", params={"path": "new", "recursive": "true"})
    assert gone.status_code == 200 and not (tree / "new").exists()
    root = rest.client.delete("/api/files", params={"path": "/"})
    assert root.status_code == 400 and tree.is_dir()
    secret = rest.client.post(
        "/api/files/upload", data={"path": ".env.local"}, files={"file": ("x", b"A=1")}
    )
    assert secret.status_code == 403 and not (tree / ".env.local").exists()


def test_upload_over_the_cap_leaves_nothing(
    rest: RestApp, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 1024)
    big = rest.client.post(
        "/api/files/upload", data={"path": "big.bin"}, files={"file": ("big.bin", b"x" * 5000)}
    )
    assert big.status_code == 413 and _code(big) == "too_large"
    assert sorted(p.name for p in tree.iterdir()) == [".env", "docs", "link-out"]


# -- files: download tickets (no credential in a URL) ---------------------------------------------


def _ticket(rest: RestApp, path: str) -> str:
    minted = rest.client.post("/api/files/download-ticket", json={"path": path})
    assert minted.status_code == 200, minted.text
    assert minted.headers["cache-control"] == "no-store"
    body = minted.json()
    assert 0 < body["expires_in"] <= 60
    return str(body["ticket"])


def _download(caller: Any, path: str, ticket: str) -> Any:
    return caller.get("/api/files/download", params={"path": path, "ticket": ticket})


def test_a_download_ticket_is_good_once_for_its_path(rest: RestApp) -> None:
    caller = rest.anonymous()  # a new tab: no header, no cookie
    ticket = _ticket(rest, "docs/a.txt")
    got = _download(caller, "docs/a.txt", ticket)
    assert got.status_code == 200 and got.content == b"hello files\n"
    assert got.headers["content-disposition"].startswith("attachment")
    again = _download(caller, "docs/a.txt", ticket)
    assert again.status_code == 401 and _code(again) == "download_ticket_invalid"


def test_a_ticket_for_one_path_opens_no_other_and_is_spent(rest: RestApp) -> None:
    caller = rest.anonymous()
    ticket = _ticket(rest, "docs/a.txt")
    other = _download(caller, "docs/blob.bin", ticket)
    assert other.status_code == 401 and _code(other) == "download_ticket_invalid"
    assert _download(caller, "docs/a.txt", ticket).status_code == 401  # spent by the misuse


def test_an_expired_ticket_is_refused(rest: RestApp) -> None:
    now = [100.0]
    rest.state.extras[files.DOWNLOADS_KEY] = DownloadTickets(monotonic=lambda: now[0])
    ticket = _ticket(rest, "docs/a.txt")
    now[0] += DOWNLOAD_TICKET_TTL_S
    late = _download(rest.anonymous(), "docs/a.txt", ticket)
    assert late.status_code == 401 and _code(late) == "download_ticket_invalid"
    with pytest.raises(ValueError):
        DownloadTickets(ttl=61)


def test_another_profile_s_dashboard_does_not_take_the_ticket(
    rest: RestApp, isolated_home: Path, tree: Path
) -> None:
    home = isolated_home / "profiles" / "play"
    home.mkdir(parents=True)
    (home / "profile.yaml").touch()
    env = EnvView.from_process().with_overlay(
        {"INDUS_HOME": str(home), files.FILES_ROOT_ENV: str(tree)}
    )
    foreign = build_rest_app(make_scope(home, env=env))
    try:
        ticket = _ticket(rest, "docs/a.txt")
        refused = _download(foreign.anonymous(), "docs/a.txt", ticket)
        assert refused.status_code == 401 and _code(refused) == "download_ticket_invalid"
        assert _download(rest.anonymous(), "docs/a.txt", ticket).status_code == 200
    finally:
        foreign.close()


def test_without_a_ticket_the_route_says_nothing_about_the_disk(rest: RestApp) -> None:
    caller = rest.anonymous()
    answers = [
        caller.get("/api/files/download", params=params)
        for params in (
            {"path": "docs/a.txt"},
            {"path": "docs/missing.txt"},
            {"path": ".env"},
            {"path": "docs/a.txt", "ticket": "x" * 43},
            {"path": "docs/a.txt", "ticket": "é"},
        )
    ]
    assert {a.status_code for a in answers} == {401}
    assert len({a.text for a in answers}) == 1
    # the session token is never taken from a URL, not even here
    token = rest.state.auth.token.value
    leaked = caller.get("/api/files/download", params={"path": "docs/a.txt", "token": token})
    assert leaked.status_code == 401 and leaked.json()["error"] == "credential_in_url"


def test_gated_mode_wants_the_cookie_and_the_ticket(scope: Scope, tree: Path) -> None:
    password_hash = hash_password("correct horse", memory_kib=1024, iterations=1, lanes=1)
    config = {"dashboard": {"basic_auth": {"username": "admin", "password_hash": password_hash}}}
    env_scope = scope.with_env(scope.env.with_overlay({files.FILES_ROOT_ENV: str(tree)}))
    built = build_rest_app(env_scope, options=AppOptions(host="0.0.0.0", config=config))
    try:
        browser = built.anonymous()
        login = {"username": "admin", "password": "correct horse"}
        assert browser.post("/auth/password-login", json=login).status_code == 200
        minted = browser.post("/api/files/download-ticket", json={"path": "docs/a.txt"})
        assert minted.status_code == 200
        ticket = minted.json()["ticket"]
        stranger = built.anonymous()
        refused = _download(stranger, "docs/a.txt", ticket)
        assert refused.status_code == 401 and refused.json()["error"] == "unauthenticated"
        got = _download(browser, "docs/a.txt", ticket)
        assert got.status_code == 200 and got.content == b"hello files\n"
        assert _download(browser, "docs/a.txt", ticket).status_code == 401
    finally:
        built.close()


def test_tickets_never_reach_a_log_or_the_audit_file(
    rest: RestApp, scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    caller = rest.anonymous()
    seen: list[str] = []
    for path in ("docs/a.txt", "docs/blob.bin"):
        ticket = _ticket(rest, path)
        seen.append(ticket)
        _download(caller, "docs/a.txt", ticket)
        _download(caller, "docs/a.txt", ticket)
    # the server's own records (the test client's request log is the browser's side)
    server = [r.getMessage() for r in caplog.records if not r.name.startswith("httpx")]
    assert server and not any(ticket in line for ticket in seen for line in server)
    for found in scope.home.rglob("*"):
        if found.is_file():
            data = found.read_bytes()
            assert not any(ticket.encode() in data for ticket in seen), found


def test_without_a_root_browsing_starts_at_home_and_needs_absolute_paths(
    scope: Scope, tmp_path: Path
) -> None:
    home = tmp_path / "user"
    (home / "notes").mkdir(parents=True)
    rest = build_rest_app(scope.with_env(scope.env.with_overlay({"HOME": str(home)})))
    try:
        listing = rest.client.get("/api/files").json()
        assert listing["path"] == str(home.resolve()) and listing["locked_root"] is None
        relative = rest.client.get("/api/files", params={"path": "notes"})
        assert relative.status_code == 400 and _code(relative) == "path_not_absolute"
        absolute = rest.client.get("/api/files", params={"path": str(home / "notes")})
        assert absolute.status_code == 200
    finally:
        rest.close()


# -- actions --------------------------------------------------------------------------------------


def test_doctor_status_streams_lines_then_done(rest: RestApp, tmp_path: Path) -> None:
    started = rest.client.post("/api/ops/doctor", params=WRITE)
    assert started.status_code == 202 and started.json()["action"] == "doctor"
    busy = rest.client.post("/api/ops/doctor", params=WRITE)
    assert busy.status_code == 409 and _code(busy) == "action_running"
    deadline = time.monotonic() + 30
    while True:
        middle = rest.client.get("/api/actions/doctor/status", params=WRITE).json()
        if "line one" in middle["lines"] or time.monotonic() > deadline:
            break
        pause(0.02)
    assert middle["state"] == "running" and "line one" in middle["lines"]
    assert "line two" not in middle["lines"]
    (tmp_path / "go").touch()
    done = _wait(rest, "doctor")
    assert done["state"] == "done" and done["exit_code"] == 0
    assert done["lines"][0].startswith("=== doctor started")
    assert done["lines"][-2:] == ["line one", "line two"]


def test_action_status_unknown_and_idle(rest: RestApp) -> None:
    assert rest.client.get("/api/actions/nope/status", params=WRITE).status_code == 404
    idle = rest.client.get("/api/actions/backup/status", params=WRITE).json()
    assert idle["state"] == "idle" and idle["lines"] == []


def test_action_log_lines_are_masked(scope: Scope, tmp_path: Path) -> None:
    store = cast(SecretStore, scope.secrets)
    stored = "sv-" + secrets.token_hex(16)
    store.set("MY_STORED_KEY", stored)
    script = f"print('value is ' + {stored[:3]!r} + {stored[3:]!r}, flush=True)"
    rest = build_rest_app(
        scope, extras={"actions.command": lambda a: [sys.executable, "-c", script]}
    )
    try:
        rest.client.post("/api/ops/doctor", params=WRITE)
        status = _wait(rest, "doctor")
        assert status["state"] == "done"
        assert all(stored not in line for line in status["lines"])
        assert any(line.startswith("value is ") for line in status["lines"])
    finally:
        rest.close()


def test_backup_runs_the_cli_and_download_is_confined(
    rest: RestApp, scope: Scope, tmp_path: Path
) -> None:
    (tmp_path / "go").touch()
    started = rest.client.post("/api/ops/backup", params=WRITE).json()
    assert started["archive"].startswith(str(scope.home / "backups"))
    assert _wait(rest, "backup")["state"] == "done"
    archive = scope.home / "backups" / "indus-backup-test.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    got = rest.client.get("/api/ops/backup/download", params={**WRITE, "archive": str(archive)})
    assert got.status_code == 200
    stray = tmp_path / "elsewhere.zip"
    stray.write_bytes(b"PK")
    refused = rest.client.get("/api/ops/backup/download", params={**WRITE, "archive": str(stray)})
    assert refused.status_code == 403


def test_import_refuses_a_missing_or_non_zip_archive(rest: RestApp, tmp_path: Path) -> None:
    missing = rest.client.post(
        "/api/ops/import", params=WRITE, json={"archive": str(tmp_path / "x.zip")}
    )
    assert missing.status_code == 404
    junk = tmp_path / "junk.zip"
    junk.write_text("not a zip", encoding="utf-8")
    refused = rest.client.post("/api/ops/import", params=WRITE, json={"archive": str(junk)})
    assert refused.status_code == 400 and _code(refused) == "not_a_zip"
    upload = rest.client.post(
        "/api/ops/import-upload", params=WRITE, files={"file": ("b.zip", b"nope")}
    )
    assert upload.status_code == 400


# -- shell hooks ----------------------------------------------------------------------------------


def test_shell_hook_needs_consent_and_is_recorded(rest: RestApp, scope: Scope) -> None:
    body = {"event": "pre_tool_call", "command": "/bin/true", "matcher": "terminal"}
    refused = rest.client.post("/api/ops/hooks", params=WRITE, json=body)
    assert refused.status_code == 400 and _code(refused) == "consent_required"
    assert rest.client.get("/api/ops/hooks", params=WRITE).json()["hooks"] == []
    made = rest.client.post("/api/ops/hooks", params=WRITE, json={**body, "consent": True})
    assert made.status_code == 201 and made.json()["approved"] is True
    config_loader.reset()
    listed = rest.client.get("/api/ops/hooks", params=WRITE).json()["hooks"]
    assert [(h["event"], h["command"], h["approved"]) for h in listed] == [
        ("pre_tool_call", "/bin/true", True)
    ]
    spec = sh.HookSpec(event="pre_tool_call", command="/bin/true")
    assert sh.ConsentStore(scope.home).granted(spec) is not None
    unknown = rest.client.post(
        "/api/ops/hooks", params=WRITE, json={"event": "nope", "command": "x", "consent": True}
    )
    assert unknown.status_code == 400
    removed = rest.client.request(
        "DELETE",
        "/api/ops/hooks",
        params=WRITE,
        json={"event": "pre_tool_call", "command": "/bin/true"},
    )
    assert removed.status_code == 200
    config_loader.reset()
    assert rest.client.get("/api/ops/hooks", params=WRITE).json()["hooks"] == []
    assert sh.ConsentStore(scope.home).granted(spec) is None


# -- credential pool, memory, updates, stats ------------------------------------------------------


def test_credential_pool_add_list_remove_by_index(rest: RestApp) -> None:
    key = "kv-" + secrets.token_hex(20)
    added = rest.client.post(
        "/api/credentials/pool",
        params=WRITE,
        json={"provider": "openrouter", "api_key": key, "label": "main"},
    )
    assert added.status_code == 201 and added.json()["index"] == 1 and key not in added.text
    listing = rest.client.get("/api/credentials/pool", params=WRITE)
    assert key not in listing.text
    pools = {p["provider"]: p for p in listing.json()["providers"]}
    entry = pools["openrouter"]["entries"][0]
    assert entry["index"] == 1 and entry["label"] == "main" and entry["preview"]
    assert entry["preview"] != key
    assert rest.client.delete("/api/credentials/pool/openrouter/5", params=WRITE).status_code == 404
    gone = rest.client.delete("/api/credentials/pool/openrouter/1", params=WRITE)
    assert gone.status_code == 200 and gone.json()["removed"] == "main"
    after = rest.client.get("/api/credentials/pool", params=WRITE).json()["providers"]
    assert "openrouter" not in {p["provider"] for p in after}
    unknown = rest.client.post(
        "/api/credentials/pool", params=WRITE, json={"provider": "nope", "api_key": key}
    )
    assert unknown.status_code == 404
    placeholder = rest.client.post(
        "/api/credentials/pool", params=WRITE, json={"provider": "openrouter", "api_key": "x"}
    )
    assert placeholder.status_code == 400


def test_memory_provider_get_and_set(rest: RestApp) -> None:
    got = rest.client.get("/api/memory", params=WRITE).json()
    assert got["active"] == "" and "MEMORY.md" in got["builtin_files"]
    unknown = rest.client.put("/api/memory/provider", params=WRITE, json={"provider": "nope"})
    assert unknown.status_code == 400
    builtin = rest.client.put("/api/memory/provider", params=WRITE, json={"provider": ""})
    assert builtin.status_code == 200 and builtin.json()["active"] == ""


def test_update_check_is_cached_and_forced(rest: RestApp, tmp_path: Path) -> None:
    probes: list[Path] = []

    def behind(folder: Path) -> int:
        probes.append(folder)
        return 3

    rest.state.extras["system.install_info"] = lambda: ("git", tmp_path)
    rest.state.extras["system.git_behind"] = behind
    first = rest.client.get("/api/system/update", params=WRITE).json()
    assert first["behind"] == 3 and first["update_available"] and first["can_apply"]
    assert first["cached"] is False
    second = rest.client.get("/api/system/update", params=WRITE).json()
    assert second["cached"] is True and len(probes) == 1
    forced = rest.client.get("/api/system/update", params={**WRITE, "force": "1"}).json()
    assert forced["cached"] is False and len(probes) == 2
    rest.state.extras["system.install_info"] = lambda: ("docker", tmp_path)
    docker = rest.client.get("/api/system/update", params=WRITE).json()
    assert docker["can_apply"] is False and docker["behind"] is None
    refused = rest.client.post("/api/system/update", params=WRITE)
    assert refused.status_code == 409 and _code(refused) == "update_not_in_place"


def test_host_stats(rest: RestApp) -> None:
    got = rest.client.get("/api/system/stats", params=WRITE).json()
    assert got["cpu_count"] == os.cpu_count()
    assert got["disk"]["total"] > 0
