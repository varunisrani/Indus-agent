"""P15-09: pairing codes, limits, lockout, per-call directory, allowlist mirror (R06.9)."""

from __future__ import annotations

import json
import logging
import stat
import sys
from pathlib import Path

import pytest

from indus.foundation import envfile
from indus.foundation.envload import env_file_path
from indus.foundation.scope import EnvView, Scope, make_scope, use_scope
from indus.state import pairing as pmod
from indus.state.pairing import (
    ALPHABET,
    CODE_LENGTH,
    CODE_TTL_SECONDS,
    LOCKOUT_SECONDS,
    MAX_PENDING_PER_PLATFORM,
    REQUEST_INTERVAL_SECONDS,
    PairingStore,
    add_revoke_listener,
    looks_like_request_id,
    normalise_code,
    pairing_dir,
)
from tests.fakes.clock import FakeClock


@pytest.fixture
def store(scope: Scope) -> PairingStore:
    return PairingStore(scope)


def _clock(scope: Scope) -> FakeClock:
    assert isinstance(scope.clock, FakeClock)
    return scope.clock


def _issue(store: PairingStore, user: str = "u1", platform: str = "telegram") -> str:
    req = store.request_code(platform, user, f"name-{user}")
    assert req.status == "issued" and req.code is not None and req.request_id is not None
    return req.code


def test_code_shape_and_alphabet(store: PairingStore, scope: Scope) -> None:
    assert len(ALPHABET) == 32 and not set("0O1I") & set(ALPHABET)
    seen: set[str] = set()
    for n in range(60):
        code = _issue(store, f"user{n}", platform=f"p{n % 20}")
        assert len(code) == CODE_LENGTH and set(code) <= set(ALPHABET)
        seen.add(code)
        _clock(scope).advance(REQUEST_INTERVAL_SECONDS)
    assert len(seen) == 60


def test_files_hold_no_code_and_modes(store: PairingStore, scope: Scope) -> None:
    code = _issue(store)
    directory = pairing_dir(scope)
    assert directory == scope.home / "platforms" / "pairing"
    for path in directory.glob("*.json"):
        assert code not in path.read_text(encoding="utf-8")
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    if sys.platform != "win32":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    pending = json.loads((directory / "telegram-pending.json").read_text(encoding="utf-8"))
    ((rid, row),) = pending.items()
    assert looks_like_request_id(rid) and len(bytes.fromhex(row["salt"])) == 16
    assert len(row["hash"]) == 64


def test_code_never_logged(store: PairingStore, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        code = _issue(store)
        store.approve_code("telegram", "WRONG000")
        store.approve_code("telegram", code)
    assert code not in caplog.text


def test_spaced_lower_case_code_approves(store: PairingStore) -> None:
    code = _issue(store)
    typed = " " + " ".join(code[i : i + 2] for i in range(0, 8, 2)).lower() + " \n"
    assert normalise_code(typed) == code
    result = store.approve_code("telegram", typed)
    assert result.approved and result.user_id == "u1" and result.user_name == "name-u1"
    assert store.is_approved("telegram", "u1")
    assert not store.is_approved("discord", "u1")
    assert store.list_pending() == []
    # A code works once.
    assert store.approve_code("telegram", code).status == "invalid"


def test_code_expires_after_ttl(store: PairingStore, scope: Scope) -> None:
    code = _issue(store)
    _clock(scope).advance(CODE_TTL_SECONDS + 1)
    assert store.approve_code("telegram", code).status == "invalid"
    assert store.list_pending() == []


def test_pending_cap_per_platform(store: PairingStore) -> None:
    for n in range(MAX_PENDING_PER_PLATFORM):
        _issue(store, f"u{n}")
    full = store.request_code("telegram", "late")
    assert full.status == "pending_full" and full.code is None and full.notify
    assert _issue(store, "late", platform="discord")


def test_one_request_per_user_per_interval_and_one_notice(
    store: PairingStore, scope: Scope
) -> None:
    _issue(store)
    first = store.request_code("telegram", "u1")
    assert first.status == "rate_limited" and first.notify
    second = store.request_code("telegram", "u1")
    assert second.status == "rate_limited" and not second.notify
    _clock(scope).advance(REQUEST_INTERVAL_SECONDS)
    again = store.request_code("telegram", "u1")
    assert again.status == "issued"


def test_lockout_after_five_wrong_refuses_a_correct_code(store: PairingStore, scope: Scope) -> None:
    code = _issue(store)
    results = [store.approve_code("telegram", "AAAAAAAA").status for _ in range(5)]
    assert results == ["invalid"] * 4 + ["locked_out"]
    sixth = store.approve_code("telegram", code)
    assert sixth.status == "locked_out" and sixth.lockout_seconds == pytest.approx(LOCKOUT_SECONDS)
    assert not store.is_approved("telegram", "u1")
    assert store.lockout_remaining("telegram") == pytest.approx(LOCKOUT_SECONDS)
    # Other platforms are not affected; code requests on the locked platform are refused.
    assert store.lockout_remaining("discord") == 0
    assert store.request_code("telegram", "u9").status == "locked_out"
    _clock(scope).advance(LOCKOUT_SECONDS + 1)
    fresh = _issue(store, "u2")
    assert store.approve_code("telegram", fresh).approved


def test_request_id_approval_ignores_lockout_and_streak(store: PairingStore) -> None:
    _issue(store, "u1")
    for _ in range(5):
        store.approve_code("telegram", "BBBBBBBB")
    assert store.lockout_remaining("telegram") > 0
    (pending,) = store.list_pending("telegram")
    assert pending.user_id == "u1" and pending.age_seconds == 0
    limits_before = json.loads((store.directory() / "_limits.json").read_text(encoding="utf-8"))
    assert store.approve_request("telegram", "0" * 16).status == "invalid"
    result = store.approve("telegram", pending.request_id.upper())
    assert result.approved and result.user_id == "u1"
    limits_after = json.loads((store.directory() / "_limits.json").read_text(encoding="utf-8"))
    assert limits_after == limits_before
    assert store.lockout_remaining("telegram") > 0


def test_success_resets_the_failure_streak(store: PairingStore) -> None:
    for _ in range(4):
        store.approve_code("telegram", "CCCCCCCC")
    assert store.approve_code("telegram", _issue(store)).approved
    for _ in range(4):
        assert store.approve_code("telegram", "CCCCCCCC").status == "invalid"
    assert store.lockout_remaining("telegram") == 0


def test_platform_names_cannot_escape(store: PairingStore) -> None:
    for bad in ("../x", "a/b", "", "UPPER!", "x" * 33):
        with pytest.raises(ValueError):
            store.request_code(bad, "u")
    assert not store.is_approved("../x", "u")


def test_directory_resolves_per_call(tmp_path: Path, fake_clock: FakeClock) -> None:
    """A store with no scope follows the current scope at each call; the gateway's code is
    approved by a CLI store resolved later from a differently spelled INDUS_HOME."""
    root = tmp_path / "root"
    home = root / "profiles" / "work"
    home.mkdir(parents=True)
    other = root / "profiles" / "play"
    other.mkdir(parents=True)
    gateway_scope = make_scope(home, env=EnvView({"INDUS_HOME": str(home)}), clock=fake_clock)
    floating = PairingStore()  # built before any scope exists (like an import-time global)
    with use_scope(gateway_scope):
        code = _issue(floating)
    with use_scope(make_scope(other, clock=fake_clock)):
        assert floating.list_pending() == []
    spelled = root / "profiles" / ".." / "profiles" / "work"
    cli_scope = make_scope(spelled, env=EnvView({"INDUS_HOME": str(spelled)}), clock=fake_clock)
    with use_scope(cli_scope):
        assert PairingStore().approve("telegram", code).approved
    assert PairingStore(gateway_scope).is_approved("telegram", "u1")


def test_unreadable_files_read_empty(store: PairingStore, caplog: pytest.LogCaptureFixture) -> None:
    directory = store.directory()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "telegram-approved.json").write_text("{broken", encoding="utf-8")
    (directory / "telegram-pending.json").write_text('{"x": 1, "y": {"hash": 1}}', encoding="utf-8")
    assert not store.is_approved("telegram", "u1")
    assert store.list_pending("telegram") == []
    assert _issue(store)


def test_list_approved_and_revoke_with_listener(store: PairingStore) -> None:
    store.approve_code("telegram", _issue(store, "u1"))
    store.approve_code("discord", _issue(store, "u2", platform="discord"))
    users = store.list_approved()
    assert {(u.platform, u.user_id) for u in users} == {("telegram", "u1"), ("discord", "u2")}
    assert [u.user_id for u in store.list_approved("discord")] == ["u2"]
    heard: list[tuple[str, str]] = []
    remove = add_revoke_listener(lambda p, u: heard.append((p, u)))
    try:
        assert store.revoke("telegram", "u1")
        assert not store.revoke("telegram", "u1")
    finally:
        remove()
    assert heard == [("telegram", "u1")]
    assert not store.is_approved("telegram", "u1")


def test_listener_errors_are_contained(store: PairingStore) -> None:
    def broken(platform: str, user: str) -> None:
        raise RuntimeError("x")

    remove = add_revoke_listener(broken)
    try:
        store.approve_code("telegram", _issue(store))
        assert store.revoke("telegram", "u1")
    finally:
        remove()


def test_clear_pending(store: PairingStore) -> None:
    _issue(store, "a")
    _issue(store, "b", platform="slack")
    assert store.clear_pending("slack") == 1
    assert store.clear_pending() == 1
    assert store.list_pending() == []


def test_allowlist_mirror_only_when_configured(store: PairingStore, scope: Scope) -> None:
    env_path = env_file_path(scope.home)
    # No allowlist configured: approval does not create one.
    store.approve_code("telegram", _issue(store, "u1"))
    assert "INDUS_TELEGRAM_ALLOWED_USERS" not in envfile.read_env_file(env_path)
    # Configured: approval adds, revoke removes.
    envfile.set_value(env_path, "INDUS_DISCORD_ALLOWED_USERS", "owner")
    store.approve_code("discord", _issue(store, "u2", platform="discord"))
    assert envfile.read_env_file(env_path)["INDUS_DISCORD_ALLOWED_USERS"] == "owner,u2"
    assert store.revoke("discord", "u2")
    assert envfile.read_env_file(env_path)["INDUS_DISCORD_ALLOWED_USERS"] == "owner"
    # A wildcard list is left alone.
    envfile.set_value(env_path, "INDUS_SLACK_ALLOWED_USERS", "*")
    store.approve_code("slack", _issue(store, "u3", platform="slack"))
    assert envfile.read_env_file(env_path)["INDUS_SLACK_ALLOWED_USERS"] == "*"


def test_allowlist_from_process_env(scope: Scope) -> None:
    scoped = scope.with_env(scope.env.with_overlay({"INDUS_TELEGRAM_ALLOWED_USERS": "a, b"}))
    store = PairingStore(scoped)
    store.approve_code("telegram", _issue(store, "c"))
    assert (
        envfile.read_env_file(env_file_path(scope.home))["INDUS_TELEGRAM_ALLOWED_USERS"] == "a,b,c"
    )


def test_decline_stamps_window(store: PairingStore, scope: Scope) -> None:
    assert not store.has_recent_decline("telegram", "x")
    store.record_decline("telegram", "x")
    assert store.has_recent_decline("telegram", "x")
    assert not store.has_recent_decline("telegram", "y")
    _clock(scope).advance(pmod.DECLINE_WINDOW_SECONDS)
    assert not store.has_recent_decline("telegram", "x")
    store.record_decline("telegram", "y")  # prunes the expired stamp
    stamps = json.loads((store.directory() / "_declined.json").read_text(encoding="utf-8"))
    assert list(stamps) == ["telegram:y"]
