"""``indus hooks`` verbs (P13-19)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from indus.foundation.scope import Scope
from indus.plugins import shell_hooks as sh
from tests.unit.cli.commands._helpers_d import run


def hook_script(folder: Path, body: str) -> str:
    path = folder / "hook.py"
    path.write_text(body, encoding="utf-8")
    return f"{json.dumps(sys.executable)} {json.dumps(str(path))}"


def configure(scope: Scope, text: str) -> None:
    (scope.home / "config.yaml").write_text(text, encoding="utf-8")


def guard_config(scope: Scope, command: str) -> sh.HookSpec:
    configure(
        scope,
        "hooks:\n  pre_tool_call:\n    - command: " + json.dumps(command) + "\n"
        "      matcher: terminal\n",
    )
    return sh.HookSpec("pre_tool_call", command, matcher="terminal")


BLOCKER = (
    "import json, sys\np = json.load(sys.stdin)\n"
    "print(json.dumps({'decision': 'block', 'reason': 'saw ' + p['tool_input']['command']}))\n"
)


def test_list_without_hooks(scope: Scope) -> None:
    result = run(scope, "hooks", "list")
    assert result.code == 0 and "No shell hooks are configured" in result.out


def test_test_prints_parsed(scope: Scope, tmp_path: Path) -> None:
    spec = guard_config(scope, hook_script(tmp_path, BLOCKER))
    skipped = run(scope, "hooks", "test", "pre_tool_call")
    assert skipped.code == 0 and "not approved: skipped" in skipped.out
    result = run(
        scope,
        "hooks",
        "test",
        "pre_tool_call",
        "--accept-hooks",
        "--payload",
        '{"args": {"command": "rm -rf build"}}',
    )
    assert result.code == 0, result.err
    assert "exit 0" in result.out
    assert 'parsed: {"action": "block", "message": "saw rm -rf build"}' in result.out
    assert sh.ConsentStore(scope.home).granted(spec) is not None
    # --for-tool filters by the matcher.
    other = run(scope, "hooks", "test", "pre_tool_call", "--for-tool", "read_file")
    assert "No shell hooks for pre_tool_call" in other.out


def test_test_with_a_failing_hook_shows_the_decision(scope: Scope, tmp_path: Path) -> None:
    guard_config(scope, str(tmp_path / "missing-hook.sh"))
    result = run(scope, "hooks", "test", "pre_tool_call", "--accept-hooks")
    assert "parsed:" in result.out and '"action": "block"' in result.out


def test_test_asks_on_a_terminal(scope: Scope, tmp_path: Path) -> None:
    spec = guard_config(scope, hook_script(tmp_path, BLOCKER))
    result = run(scope, "hooks", "test", "pre_tool_call", tty=True, input="y\n")
    assert result.code == 0, result.err
    assert "runs with your user rights" in result.out and "parsed:" in result.out
    assert sh.ConsentStore(scope.home).granted(spec) is not None


def test_unknown_event_is_a_usage_error(scope: Scope) -> None:
    result = run(scope, "hooks", "test", "pre_tool")
    assert result.code == 2 and "unknown event" in result.err


def test_revoke_then_list_shows_not_granted(scope: Scope, tmp_path: Path) -> None:
    spec = guard_config(scope, hook_script(tmp_path, BLOCKER))
    sh.ConsentStore(scope.home).grant(spec)
    listed = run(scope, "hooks", "list")
    assert f"[{spec.grant_id}]" in listed.out and "approved 20" in listed.out
    assert run(scope, "hooks", "revoke", "deadbeef").code == 1
    revoked = run(scope, "hooks", "revoke", spec.grant_id)
    assert revoked.code == 0 and f"Approval {spec.grant_id} removed" in revoked.out
    assert "not approved" in run(scope, "hooks", "list").out


def test_list_warns_when_the_script_changed(scope: Scope, tmp_path: Path) -> None:
    path = tmp_path / "guard.sh"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    spec = guard_config(scope, str(path))
    sh.ConsentStore(scope.home).grant(spec)
    path.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 100))
    assert "script changed since it was approved" in run(scope, "hooks", "list").out


def test_doctor(scope: Scope, tmp_path: Path) -> None:
    assert run(scope, "hooks", "doctor").code == 0
    path = tmp_path / "guard.sh"
    path.write_text("#!/bin/sh\ncat >/dev/null\necho '{}'\n", encoding="utf-8")
    path.chmod(0o755)
    spec = guard_config(scope, str(path))
    unapproved = run(scope, "hooks", "doctor")
    assert unapproved.code == 0 and "not approved yet" in unapproved.out
    sh.ConsentStore(scope.home).grant(spec)
    healthy = run(scope, "hooks", "doctor")
    assert healthy.code == 0 and "exit 0" in healthy.out and "healthy" in healthy.out
    path.chmod(0o644)
    broken = run(scope, "hooks", "doctor")
    assert broken.code == 1 and "is not executable" in broken.out
