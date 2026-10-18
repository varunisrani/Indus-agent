"""Approval floors: interpreter delete, sudo -S guessing, deny-glob spellings (P05-08)."""

from __future__ import annotations

import pytest

from indus.safety.approvals.detect import detect
from indus.safety.approvals.floors import (
    deny_variants,
    hardline_hit,
    match_deny_glob,
    runtime_delete_target,
    sudo_stdin_guess,
)

ROOTS = ("/opt/app/.venv", "/opt/app/.venv/bin/python3")


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        ("rm -rf /opt/app/.venv", None),
        ("rm -r -f /opt/app", None),
        ("rm -rf /opt/app/.ven*", None),
        ("rm /opt/app/.venv/bin/python3", None),
        ("sudo rm -rf /opt/app/.venv/", None),
        ("rm -rf .venv", "/opt/app"),
        ("rm -rf ..", "/opt/app/src"),
        ("find /opt/app -name '*.pyc' -delete", None),
        ("bash -c 'rm -rf /opt/app/.venv'", None),
        ("cd /tmp && unlink /opt/app/.venv/bin/python3", None),
    ],
)
def test_runtime_delete_detected(command: str, cwd: str | None) -> None:
    assert runtime_delete_target(command, roots=ROOTS, cwd=cwd) is not None


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        ("rm -rf .venv", None),  # relative and no cwd known
        ("rm -rf /opt/app/build", None),
        ("rm -rf /opt/other/.venv", None),
        ("ls /opt/app/.venv", None),
        ('git commit -m "rm -rf /opt/app/.venv"', None),
        ("cat <<EOF > notes.txt\nrm -rf /opt/app/.venv\nEOF", None),
        ("rm -rf /", None),  # hardline handles the filesystem root
    ],
)
def test_runtime_delete_not_detected(command: str, cwd: str | None) -> None:
    assert runtime_delete_target(command, roots=ROOTS, cwd=cwd) is None


def test_runtime_delete_uses_home_for_tilde() -> None:
    roots = ("/home/u/.indus/venv",)
    assert runtime_delete_target("rm -rf ~/.indus", roots=roots, home="/home/u") is not None
    assert runtime_delete_target("rm -rf ~/.indus", roots=roots) is None


@pytest.mark.parametrize(
    "command",
    [
        "sudo -S apt-get update",
        "echo guess | sudo -S true",
        "sudo -kS ls /root",
        "sudo --stdin id",
        "sudo -u root -S id",
        "env LANG=C sudo -S id",
        "bash -c 'echo guess | sudo -S id'",
    ],
)
def test_sudo_stdin_guess(command: str) -> None:
    assert sudo_stdin_guess(command)


@pytest.mark.parametrize(
    "command",
    ["sudo apt-get update", "sudo ls -S /var", "sort -S 1G file", 'git commit -m "sudo -S"'],
)
def test_sudo_without_stdin_flag(command: str) -> None:
    assert not sudo_stdin_guess(command)


def test_deny_glob_variants() -> None:
    globs = ["rm -rf *", "git push*"]
    assert match_deny_glob("RM -RF /tmp/x", globs) == "rm -rf *"
    assert match_deny_glob("r''m -rf /tmp/x", globs) == "rm -rf *"
    assert match_deny_glob("sudo -u root rm -rf /tmp/x", globs) == "rm -rf *"
    assert match_deny_glob("FOO=1 env rm  -rf  /tmp/x", globs) == "rm -rf *"
    assert match_deny_glob("cd repo && git   push --force", globs) == "git push*"
    assert match_deny_glob("ls -la", globs) is None
    assert match_deny_glob("rm -rf /tmp/x", ["", 3, None]) is None
    assert "rm -rf /tmp/x" in deny_variants("sudo rm -rf /tmp/x")


def test_hardline_hit_wording() -> None:
    hit = hardline_hit(detect("mkfs.ext4 /dev/sda1", user_home="/home/u", indus_home="/x"))
    assert hit is not None and hit.key == "mkfs"
    assert "whatever the approval settings" in hit.message
    assert hardline_hit(detect("ls", user_home="/home/u", indus_home="/x")) is None
