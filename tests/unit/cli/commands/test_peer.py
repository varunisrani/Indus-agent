"""P24-07: ``indus peer`` add / list / remove / dm / run / status / stop (non-TTY)."""

from __future__ import annotations

import io
import json

import pytest

from indus.foundation.scope import Scope
from indus.service.peers import PEERS_FILE, PeerBook, key_name
from tests.unit.cli.commands.cmdrun import Ran, run_cli


def _run(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Ran:
    return run_cli(capsys, scope, "peer", *args)


def _key() -> str:
    return "peer-" + "q" * 28  # built at runtime


def test_list_is_the_default_and_starts_empty(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = _run(capsys, scope)
    assert ran.code == 0 and "No peers yet" in ran.out
    assert _run(capsys, scope, "ls").out == ran.out


def test_add_with_a_key_from_stdin(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _key()
    monkeypatch.setattr("sys.stdin", io.StringIO(key + "\n"))
    ran = _run(capsys, scope, "add", "Spark", "--url", "http://spark.example:8642/", "--key-stdin")
    assert ran.code == 0 and "Added peer 'spark'" in ran.out
    assert key not in ran.out + ran.err
    assert scope.secrets is not None and scope.secrets.get(key_name("spark")) == key
    assert key not in (scope.home / PEERS_FILE).read_text(encoding="utf-8")
    listed = json.loads(_run(capsys, scope, "list", "--json").out)
    assert listed == {
        "peers": [
            {"name": "spark", "url": "http://spark.example:8642", "lan": False, "has_key": True}
        ]
    }


def test_add_without_a_key_and_without_a_terminal_says_how(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = _run(capsys, scope, "add", "lab", "--url", "http://10.1.2.3:8642", "--lan")
    assert ran.code == 0 and "no key yet" in ran.out and key_name("lab") in ran.out
    assert "[no key (INDUS_PEER_LAB_KEY), lan]" in _run(capsys, scope, "list").out


def test_add_refuses_a_bad_name_or_url(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    assert _run(capsys, scope, "add", "bad name", "--url", "http://h:1").code == 2
    ran = _run(capsys, scope, "add", "lab", "--url", "http://user:pw@h:1")
    assert ran.code == 2 and "user name or password" in ran.err
    assert PeerBook(scope).load() == {}


def test_remove_forgets_the_peer_and_its_key(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(_key() + "\n"))
    _run(capsys, scope, "add", "spark", "--url", "http://spark.example:1", "--key-stdin")
    ran = _run(capsys, scope, "rm", "spark")
    assert ran.code == 0 and "Removed peer 'spark'" in ran.out
    assert scope.secrets is not None and scope.secrets.get(key_name("spark")) is None
    assert _run(capsys, scope, "remove", "spark").code == 1


def test_dm_without_a_key_exits_1_with_guidance(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    _run(capsys, scope, "add", "spark", "--url", "http://spark.example:8642")
    ran = _run(capsys, scope, "dm", "spark", "hello")
    assert ran.code == 1
    assert "no API key" in ran.err and "[reason: missing_config]" in ran.err


def test_dm_usage_errors(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    assert _run(capsys, scope, "dm").code == 2
    assert _run(capsys, scope, "dm", "spark").code == 2
    assert _run(capsys, scope, "dm", "spark/bad name", "hi").code == 2
    assert _run(capsys, scope, "dm", "spark", "   ").code == 2


def test_dm_unknown_peer(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _run(capsys, scope, "dm", "ghost", "hello")
    assert ran.code == 1 and "indus peer add ghost" in ran.err
