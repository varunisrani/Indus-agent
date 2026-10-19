"""P15-09: ``indus pairing`` list / approve / revoke / clear-pending (non-TTY)."""

from __future__ import annotations

import json

import pytest

from indus.foundation.scope import Scope
from indus.state.pairing import PairingStore
from tests.unit.cli.commands.cmdrun import Ran, run_cli


def _run(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Ran:
    return run_cli(capsys, scope, "pairing", *args)


def _issue(scope: Scope, user: str = "42", platform: str = "telegram") -> str:
    req = PairingStore(scope).request_code(platform, user, "Ada")
    assert req.code is not None
    return req.code


def test_list_empty(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _run(capsys, scope)
    assert ran.code == 0 and "Nobody has asked to pair yet" in ran.out
    assert _run(capsys, scope, "ls").out == ran.out


def test_list_shows_requests_without_codes(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    code = _issue(scope)
    ran = _run(capsys, scope, "list")
    assert ran.code == 0
    assert "Waiting for approval (1)" in ran.out and "telegram" in ran.out and "Ada" in ran.out
    assert code not in ran.out
    (pending,) = PairingStore(scope).list_pending()
    assert pending.request_id in ran.out
    data = json.loads(_run(capsys, scope, "list", "--json").out)
    assert data["pending"][0]["request_id"] == pending.request_id
    assert data["approved"] == [] and code not in json.dumps(data)


def test_approve_by_spaced_code(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    code = _issue(scope)
    ran = _run(capsys, scope, "approve", "Telegram", code[:4].lower(), code[4:])
    assert ran.code == 0 and "Paired Ada (42) on telegram" in ran.out
    assert PairingStore(scope).is_approved("telegram", "42")
    listed = _run(capsys, scope, "list")
    assert "Paired users (1)" in listed.out and "No requests are waiting" in listed.out


def test_approve_by_request_id(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    _issue(scope)
    (pending,) = PairingStore(scope).list_pending()
    ran = _run(capsys, scope, "approve", "telegram", pending.request_id)
    assert ran.code == 0 and PairingStore(scope).is_approved("telegram", "42")


def test_unknown_code(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _run(capsys, scope, "approve", "telegram", "ZZZZZZZZ")
    assert ran.code == 1 and "No waiting request on telegram" in ran.err


def test_locked_out_message(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    code = _issue(scope)
    for _ in range(5):
        _run(capsys, scope, "approve", "telegram", "WRONGWRO")
    ran = _run(capsys, scope, "approve", "telegram", code)
    assert ran.code == 1 and "paused for about 60 more minute" in ran.err
    assert "request id" in ran.err


def test_bad_platform_is_a_usage_error(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _run(capsys, scope, "approve", "../x", "ABCDEFGH")
    assert ran.code == 2 and "not a platform name" in ran.err


def test_revoke(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    store = PairingStore(scope)
    store.approve_code("telegram", _issue(scope))
    ran = _run(capsys, scope, "revoke", "telegram", "42")
    assert ran.code == 0 and "Revoked 42 on telegram" in ran.out
    again = _run(capsys, scope, "revoke", "telegram", "42")
    assert again.code == 1 and "is not paired" in again.err


def test_clear_pending(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    _issue(scope)
    _issue(scope, "7", platform="slack")
    ran = _run(capsys, scope, "clear-pending", "slack")
    assert ran.code == 0 and "Dropped 1" in ran.out
    assert _run(capsys, scope, "clear-pending").out.startswith("Dropped 1")
    assert "No requests were waiting" in _run(capsys, scope, "clear-pending").out
