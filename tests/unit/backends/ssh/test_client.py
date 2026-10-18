"""SSH client argv and control sockets (P11-08)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from indus.backends.ssh.client import (
    SOCKET_PATH_LIMIT,
    SSH_TEMP_SUFFIX,
    SshTarget,
    control_dir,
    control_path,
    exit_argv,
    multiplex_supported,
    scp_argv,
    send_env_names,
    socket_dir_usable,
    ssh_argv,
)
from tests.fakes.terminal import forge_token

TARGET = SshTarget(host="build.example.test", user="dev", port=2222, key_path="/keys/id_ed25519")


def test_argv_golden() -> None:
    argv = ssh_argv("ssh", TARGET, "bash -c 'true'", control="/run/indus-ssh/abc")
    assert argv == [
        "ssh",
        "-o",
        "ControlPath=/run/indus-ssh/abc",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=300",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-p",
        "2222",
        "-i",
        "/keys/id_ed25519",
        "dev@build.example.test",
        "bash -c 'true'",
    ]


def test_argv_without_key_user_or_multiplexing() -> None:
    argv = ssh_argv("ssh", SshTarget(host="h"), "echo", control=None)
    assert argv == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-p",
        "22",
        "h",
        "echo",
    ]


def test_sendenv_names_sorted_and_checked() -> None:
    argv = ssh_argv("ssh", TARGET, "x", control=None, send_env=["ZED", "ALPHA", "bad-name", "ZED"])
    pairs = [argv[i + 1] for i, word in enumerate(argv) if word == "-o"]
    assert [p for p in pairs if p.startswith("SendEnv=")] == ["SendEnv=ALPHA", "SendEnv=ZED"]
    assert send_env_names(["B", "A", "", "1X"]) == ("A", "B")


def test_scp_and_exit_argv() -> None:
    assert scp_argv("scp", TARGET, "/a b", "/r/x", control="/c") == [
        "scp",
        "-o",
        "ControlPath=/c",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-P",
        "2222",
        "-i",
        "/keys/id_ed25519",
        "-q",
        "/a b",
        "dev@build.example.test:/r/x",
    ]
    assert exit_argv("ssh", TARGET, "/c") == [
        "ssh",
        "-o",
        "ControlPath=/c",
        "-O",
        "exit",
        "dev@build.example.test",
    ]


def test_socket_path_fits_the_macos_limit() -> None:
    long_host = SshTarget(host="h" * 250 + ".example.test", user="u" * 64, port=65535)
    deep_runtime = "/var/folders/xy/" + "z" * 80 + "/T"
    directory = control_dir(deep_runtime, "/Users/someone/.indus/cache/scratch")
    path = control_path(directory, long_host, ["A_TOKEN", "B_TOKEN"])
    assert len(path.encode()) + SSH_TEMP_SUFFIX < SOCKET_PATH_LIMIT
    assert directory.endswith("/indus-ssh")
    # A short runtime dir is used as it is.
    assert control_dir("/run/user/1000", "/x") == "/run/user/1000/indus-ssh"
    assert control_dir(None, "relative/dir") == "/tmp/indus-ssh"


def test_each_sendenv_set_gets_its_own_socket() -> None:
    plain = control_path("/d", TARGET)
    one = control_path("/d", TARGET, ["API_TOKEN"])
    two = control_path("/d", TARGET, ["API_TOKEN", "OTHER"])
    assert len({plain, one, two}) == 3
    assert one.startswith(plain + "-")
    assert control_path("/d", TARGET, ["OTHER", "API_TOKEN"]) == two
    other_port = SshTarget(host=TARGET.host, user=TARGET.user, port=22)
    assert control_path("/d", other_port) != plain


def test_windows_has_no_multiplexing() -> None:
    assert not multiplex_supported("nt")
    assert multiplex_supported("posix")


def test_secret_value_never_in_argv() -> None:
    secret = forge_token()
    client_env = {"API_TOKEN": secret}
    argv = ssh_argv("ssh", TARGET, "printenv API_TOKEN", control="/c", send_env=list(client_env))
    assert "-o" in argv and "SendEnv=API_TOKEN" in argv
    assert not any(secret in word for word in argv)


def test_socket_dir_is_private(tmp_path: Path) -> None:
    good = tmp_path / "mine" / "indus-ssh"
    assert socket_dir_usable(good)
    assert stat.S_IMODE(good.stat().st_mode) == 0o700
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o777)
    os.chmod(loose, 0o777)
    assert socket_dir_usable(loose)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700
    assert not socket_dir_usable(good, uid=os.getuid() + 1)
