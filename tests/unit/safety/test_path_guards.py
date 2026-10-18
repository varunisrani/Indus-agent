"""Read and write path rules for the file tools (P05-16, P05-18)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.safety import path_guards as pg


@pytest.fixture
def home_scope(scope: Scope, tmp_path: Path) -> Scope:
    home = tmp_path / "user"
    home.mkdir()
    return scope.with_env(scope.env.with_overlay({"HOME": str(home)}))


def user_home(scope: Scope) -> str:
    return os.path.realpath(scope.env["HOME"])


@pytest.mark.parametrize(
    "raw",
    [
        "\\??\\C:\\x",
        "\\\\.\\PhysicalDrive0",
        "\\\\?\\UNC\\host\\share",
        "//./pipe/x",
        "\\\\?\\GLOBALROOT\\x",
    ],
)
def test_nt_namespace_prefixes_are_refused(raw: str) -> None:
    assert pg.nt_namespace_error(raw, verb="Read")


def test_ordinary_paths_are_not_nt_namespaces() -> None:
    assert pg.nt_namespace_error("C:\\Users\\me\\x.txt") is None
    assert pg.nt_namespace_error("/home/me/x") is None


@pytest.mark.parametrize(
    "path",
    [
        "/dev/zero",
        "/dev/urandom",
        "/dev/stdin",
        "/dev/fd/0",
        "/dev/tty",
        "/proc/self/environ",
        "/proc/1234/cmdline",
        "/proc/1/task/9/maps",
        "/proc/self/smaps_rollup",
        "/proc/self/fd/2",
        "/dev/./zero",
    ],
)
def test_device_and_proc_leak_paths(path: str) -> None:
    assert pg.is_device_path(path)


@pytest.mark.parametrize("path", ["/dev/null", "/proc/cpuinfo", "/proc/self/status", "/tmp/x"])
def test_harmless_special_paths_pass(path: str) -> None:
    assert not pg.is_device_path(path)


def test_device_reached_through_a_symlink_chain(tmp_path: Path) -> None:
    (tmp_path / "hop2").symlink_to("/proc/self/environ")
    (tmp_path / "hop1").symlink_to(tmp_path / "hop2")
    assert pg.blocked_device(str(tmp_path / "hop1"))
    assert not pg.blocked_device(str(tmp_path / "hop1"), follow_links=False)


def test_read_denials(scope: Scope, tmp_path: Path) -> None:
    home = os.path.realpath(scope.home)
    root = os.path.realpath(scope.root)
    assert pg.read_denial(f"{home}/secrets.json", scope)
    assert pg.read_denial(f"{root}/auth.json", scope)
    assert pg.read_denial(f"{home}/config.yaml", scope)
    assert pg.read_denial(str(tmp_path / "proj" / ".env"), scope)
    assert pg.read_denial(str(tmp_path / "proj" / ".env.production"), scope)
    assert pg.read_denial(str(tmp_path / "proj" / ".envrc"), scope)
    assert pg.read_denial(str(tmp_path / "proj" / ".env.example"), scope) is None
    assert pg.read_denial(str(tmp_path / "proj" / "config.yaml"), scope) is None
    assert pg.read_denial(f"{home}/PERSONA.md", scope) is None


def test_write_denials(home_scope: Scope) -> None:
    home = user_home(home_scope)
    indus = os.path.realpath(home_scope.home)
    for path in (
        "/etc/passwd",
        "/boot/grub/grub.cfg",
        "/usr/lib/systemd/system/x.service",
        "/var/run/docker.sock",
        f"{home}/.ssh/id_ed25519",
        f"{home}/.ssh/authorized_keys",
        f"{home}/.aws/credentials",
        f"{home}/.kube/config",
        f"{home}/.netrc",
        f"{indus}/secrets.json",
        f"{indus}/config.yaml",
        f"{indus}/.env",
    ):
        assert pg.write_denial([path], home_scope), path
    assert pg.write_denial([f"{home}/.ssh/config"], home_scope) is None
    assert pg.write_denial([f"{home}/project/main.py"], home_scope) is None
    assert pg.write_denial([f"{home}/.sshx/file"], home_scope) is None


def test_ssh_config_needs_approval(home_scope: Scope) -> None:
    home = user_home(home_scope)
    assert pg.write_needs_approval([f"{home}/.ssh/config"], home_scope)
    assert pg.write_needs_approval([f"{home}/notes.txt"], home_scope) is None


def test_binary_documents() -> None:
    assert pg.binary_document_reason("/w/report.docx", exists=False)
    assert pg.binary_document_reason("/w/Deck.PPTX", exists=False)
    assert pg.binary_document_reason("/w/data.db-wal", exists=False)
    assert pg.binary_document_reason("/w/new.pdf", exists=False) is None
    assert pg.binary_document_reason("/w/old.pdf", exists=True)
    assert pg.binary_document_reason("/w/fixture.db", exists=False) is None
    assert pg.binary_document_reason("/w/app.sqlite", exists=True)
    assert pg.binary_document_reason("/w/main.py", exists=True) is None


def test_protected_instruction_names_any_case_and_globs() -> None:
    assert pg.protected_instruction_name(["/w/AGENTS.md"]) == "AGENTS.md"
    assert pg.protected_instruction_name(["/w/sub/claude.MD"]) == "claude.MD"
    assert pg.protected_instruction_name(["/w/.cursorrules"]) == ".cursorrules"
    assert pg.protected_instruction_name(["/home/x/.indus/PERSONA.md"]) == "PERSONA.md"
    assert pg.protected_instruction_name(["/w/README.md"]) is None
    assert pg.protected_instruction_name(["/w/Rules.TXT"], ["rules.*"]) == "Rules.TXT"


def test_binary_extension_table() -> None:
    assert pg.has_binary_extension("/a/photo.JPG")
    assert pg.has_binary_extension("/a/x.sqlite3-journal")
    assert not pg.has_binary_extension("/a/doc.pdf")
    assert not pg.has_binary_extension("/a/.env")
    assert pg.is_opaque_document("/a/book.epub")
