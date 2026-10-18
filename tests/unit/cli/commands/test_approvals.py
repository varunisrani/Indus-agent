"""P07-18: `indus approvals test` — dry verdicts from the real P05 pipeline."""

from __future__ import annotations

import json

import pytest

from indus.backends.contract import BackendFlags
from indus.backends.registry import BackendEntry
from indus.foundation import config
from indus.foundation.scope import Scope
from indus.safety.approvals import APPROVALS
from indus.tools.builtin import terminal
from tests.unit.cli.commands.cmdrun import run_cli, write_settings


def _verdict(
    capsys: pytest.CaptureFixture[str], scope: Scope, *args: str
) -> tuple[int, dict[str, object]]:
    ran = run_cli(capsys, scope, "approvals", "test", "--json", *args)
    return ran.code, json.loads(ran.out) if ran.out.strip() else {}


@pytest.mark.parametrize(
    ("command", "code", "verdict"),
    [
        ("rm -rf /", 3, "deny"),  # hardline floor
        ("mkfs.ext4 /dev/sda1", 3, "deny"),
        ("git push --force origin main", 2, "ask"),  # dangerous → a person would be asked
        ("chmod -R 777 /srv/app", 2, "ask"),
        ("ls -la", 0, "allow"),
        ("echo hello && pwd", 0, "allow"),
        ("deploy-now --prod", 3, "deny"),  # user deny glob
        ("git push --force origin feature", 0, "allow"),  # allowlist (exact text)
    ],
)
def test_verdict_table(
    capsys: pytest.CaptureFixture[str], scope: Scope, command: str, code: int, verdict: str
) -> None:
    write_settings(
        scope,
        "approvals:\n  deny: ['deploy-now *']\n"
        "command_allowlist: ['git push --force origin feature']\n",
    )
    got_code, body = _verdict(capsys, scope, "--", *command.split())
    assert (got_code, body["verdict"]) == (code, verdict)
    assert body["exit_code"] == code


def test_mode_off_and_yolo_allow_but_floors_hold(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    write_settings(scope, "approvals:\n  mode: 'off'\n")
    assert _verdict(capsys, scope, "--", "git", "push", "--force")[0] == 0
    assert _verdict(capsys, scope, "--", "rm", "-rf", "/")[0] == 3
    write_settings(scope, "approvals:\n  mode: manual\n")
    yolo = scope.with_env(scope.env.with_overlay({"INDUS_YOLO_MODE": "1"}))
    code, body = _verdict(capsys, yolo, "--", "git", "push", "--force")
    assert (code, body["step"]) == (0, "auto")


def test_json_shape(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    code, body = _verdict(capsys, scope, "--", "git", "push", "--force")
    assert code == 2
    assert set(body) == {
        "command",
        "backend",
        "verdict",
        "exit_code",
        "step",
        "outcome",
        "rule",
        "description",
        "trace",
    }
    assert body["command"] == "git push --force"
    assert body["backend"] == "local"
    assert body["rule"]
    assert isinstance(body["trace"], list) and body["trace"][0] == "git push --force"


def test_text_report(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "approvals", "test", "--", "rm", "-rf", "/")
    assert ran.code == 3
    assert "Verdict: deny (exit 3)" in ran.out
    assert "Rule:" in ran.out
    assert "Checked as:" in ran.out


def test_isolated_backend_skips_all_but_deny(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    registry = terminal.runtime().environments
    registry.register(
        BackendEntry("sandbox", registry.entry("local").factory, BackendFlags(isolated=True))  # type: ignore[union-attr]
    )
    write_settings(scope, "approvals:\n  deny: ['deploy-now *']\n")
    assert _verdict(capsys, scope, "--backend", "sandbox", "--", "git", "push", "--force")[0] == 0
    assert _verdict(capsys, scope, "--backend", "sandbox", "--", "deploy-now", "x")[0] == 3
    assert _verdict(capsys, scope, "--backend", "sandbox", "--", "rm", "-rf", "/")[0] == 3


def test_nothing_written(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    path = write_settings(scope, "approvals:\n  mode: manual\n")
    before = path.read_bytes()
    config.load(scope)  # the loader keeps its own last-known-good copy; that is not ours
    files_before = sorted(p.name for p in scope.home.iterdir())
    _verdict(capsys, scope, "--", "git", "push", "--force")
    assert path.read_bytes() == before
    assert sorted(p.name for p in scope.home.iterdir()) == files_before
    assert not APPROVALS.pattern_approved((str(scope.home), "approvals-test"), "git_destructive")


def test_no_command_is_usage(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "approvals", "test")
    assert ran.code == 1
    assert "Usage:" in ran.err
    ran = run_cli(capsys, scope, "approvals", "test", "--backend", "nowhere", "--", "ls")
    assert ran.code == 1
    assert "Unknown backend" in ran.err


def test_bare_group_prints_help(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = run_cli(capsys, scope, "approvals")
    assert ran.code == 0
    assert "test" in ran.out
