"""P18-03: the auth audit log carries no secret, rotates, and never breaks a login."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

from indus.web.auth import cookies
from indus.web.auth.audit import AUDIT_FILE, AuditEvent, AuditLog
from tests.unit.web.conftest import Builder, basic_config


def _fake_key() -> str:
    return "sk" + "-" + "proj" + "A1b2C3d4" * 6


def test_secret_fields_are_dropped_and_shapes_masked(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "logs" / AUDIT_FILE, secrets=["exact-session-token-value-123"])
    log.record(
        AuditEvent.LOGIN_FAILURE,
        provider="basic",
        password="hunter2-hunter2",
        access_token="at-value",
        refresh_token="rt-value",
        code="code-value",
        code_verifier="verifier-value",
        state="state-value",
        Authorization="Bearer abc",
        ticket="ticket-value",
        reason=f"saw {_fake_key()} and exact-session-token-value-123",
        nested={"cookie": "c-value", "ok": "fine"},
    )
    text = (tmp_path / "logs" / AUDIT_FILE).read_text(encoding="utf-8")
    entry = json.loads(text)
    assert entry["event"] == "login_failure" and entry["provider"] == "basic"
    for leaked in (
        "hunter2",
        "at-value",
        "rt-value",
        "code-value",
        "verifier-value",
        "state-value",
        "Bearer abc",
        "ticket-value",
        _fake_key(),
        "exact-session-token-value-123",
        "c-value",
    ):
        assert leaked not in text, leaked
    assert entry["nested"] == {"ok": "fine"}


def test_file_is_owner_only_and_rotates(tmp_path: Path) -> None:
    path = tmp_path / "logs" / AUDIT_FILE
    log = AuditLog(path, max_bytes=400)
    for index in range(30):
        log.record(AuditEvent.LOGIN_START, provider="basic", n=index, pad="x" * 40)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.with_name(f"{AUDIT_FILE}.1").exists()
    assert not path.with_name(f"{AUDIT_FILE}.4").exists()
    assert path.stat().st_size < 600


def test_a_broken_log_never_raises(tmp_path: Path) -> None:
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory", encoding="utf-8")
    AuditLog(blocked / AUDIT_FILE).record(AuditEvent.LOGOUT, provider="basic")


def test_a_login_flow_leaves_no_secret_in_the_log(build: Builder, scope: object) -> None:
    password = "correct horse"
    config = basic_config(password=password)
    built = build(host="0.0.0.0", config=config)
    built.client.post("/auth/password-login", json={"username": "admin", "password": "wrong one"})
    login = built.client.post(
        "/auth/password-login", json={"username": "admin", "password": password}
    )
    session = login.cookies.get(cookies.AT_COOKIE) or ""
    assert session
    built.client.post("/api/auth/ws-ticket", json={"purpose": "web-pty"})
    built.client.post("/auth/logout")
    text = built.state.auth.audit.path.read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in text.splitlines()]
    assert {"login_failure", "login_success", "ws_ticket_minted", "logout"} <= set(events)
    secrets = [
        password,
        "wrong one",
        session,
        session.split(".")[-1],
        built.token,
        config["dashboard"]["basic_auth"]["password_hash"],
    ]
    for value in secrets:
        assert value not in text
    assert not re.search(r"v1\.[A-Za-z0-9_-]{20,}\.", text)
