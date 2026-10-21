"""install.sh runtime stages (P23-05, R10.16): pinned uv with mirror fallback, libc detection,
dependencies, config, products and PATH wiring, setup without a terminal, the complete marker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.install.fake_tools import (
    FAKE_UV,
    INSTALL_SH,
    OFFICIAL_URL,
    ROOT,
    Sandbox,
    elf_header,
    make_origin,
    uv_archive,
)
from tests.integration.install.stage_protocol import parse_stage_frame

pytestmark = pytest.mark.platforms("posix")

UPSTREAM = "https://upstream.example.test/uv-x86_64-unknown-linux-gnu.tar.gz"
MIRROR = "https://github.com/indusagi/indus-agent/releases/download/bootstrap-mirror/uv.tar.gz"
UV_VERSION = re.search(
    r"^INS_UV_VERSION='([^']+)'$", INSTALL_SH.read_text(encoding="utf-8"), re.MULTILINE
)


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _uv_entry(box: Sandbox) -> Path:
    assert UV_VERSION is not None
    return box.indus_home / "tools" / f"uv-{UV_VERSION.group(1)}-linux-x64"


def _pin_prelude(digest: str) -> str:
    return (
        "ins_uv_target() { printf linux-x64; }\n"
        f"ins_uv_pin() {{ INS_PIN_URL='{UPSTREAM}'; INS_PIN_MIRROR='{MIRROR}'; "
        f"INS_PIN_SHA256='{digest}'; }}\n"
    )


def _stage(
    box: Sandbox, name: str, *extra: str, prelude: str = "", env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any], str]:
    result = box.run(
        "--stage",
        name,
        "--json",
        "--indus-home",
        str(box.indus_home),
        *extra,
        prelude=prelude,
        env=env,
    )
    frame = parse_stage_frame(result.stdout)
    assert frame is not None, result.stdout + result.stderr
    return result.returncode, frame, result.stdout + result.stderr


def _uv_env(box: Sandbox, **extra: str) -> dict[str, str]:
    return box.env(FAKE_UV_PYTHON=sys.executable, **extra)


def _archive(box: Sandbox, script: str = FAKE_UV, name: str = "uv") -> tuple[Path, str]:
    path = box.root / f"{name}-archive.tar.gz"
    data = uv_archive(path, script, name=name)
    return path, hashlib.sha256(data).hexdigest()


# -- the pinned uv --------------------------------------------------------------------------------


def test_network_failure_uses_the_mirror(box: Sandbox) -> None:
    archive, digest = _archive(box)
    box.rule(tool="curl", match=[UPSTREAM], exit=22)
    box.rule(tool="curl", match=[MIRROR], body_file=str(archive))
    code, frame, out = _stage(box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box))
    assert code == 0 and frame["ok"], out
    assert [c[-1] for c in box.calls("curl")] == [UPSTREAM, MIRROR]
    assert os.access(_uv_entry(box) / "uv", os.X_OK)
    assert (_uv_entry(box) / "uvx").exists()
    assert "trying our mirror" in out


def test_a_bad_body_with_exit_0_is_a_digest_mismatch_and_no_mirror(box: Sandbox) -> None:
    _, digest = _archive(box)
    box.rule(tool="curl", match=[UPSTREAM], body="<html>404 Not Found</html>")
    box.rule(tool="curl", match=[MIRROR], exit=99)
    code, frame, _ = _stage(box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box))
    assert code == 1
    assert str(frame["reason"]).startswith(f"the uv download from {UPSTREAM} does not match")
    assert [c[-1] for c in box.calls("curl")] == [UPSTREAM]
    assert not _uv_entry(box).exists()


@pytest.mark.parametrize("code", [5, 6, 7, 18, 22, 28, 52, 55, 56])
def test_every_network_code_permits_the_mirror(box: Sandbox, code: int) -> None:
    archive, digest = _archive(box)
    box.rule(tool="curl", match=[UPSTREAM], exit=code)
    box.rule(tool="curl", match=[MIRROR], body_file=str(archive))
    result, frame, out = _stage(box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box))
    assert result == 0 and frame["ok"], out


@pytest.mark.parametrize("code", [1, 3, 23, 35, 60])
def test_other_curl_failures_never_try_the_mirror(box: Sandbox, code: int) -> None:
    _, digest = _archive(box)
    box.rule(tool="curl", match=[UPSTREAM], exit=code)
    result, frame, _ = _stage(box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box))
    assert result == 1
    assert f"(curl exit {code})" in str(frame["reason"])
    assert [c[-1] for c in box.calls("curl")] == [UPSTREAM]


def test_a_mirror_with_bad_bytes_is_refused_too(box: Sandbox) -> None:
    _, digest = _archive(box)
    box.rule(tool="curl", match=[UPSTREAM], exit=7)
    box.rule(tool="curl", match=[MIRROR], body="tampered")
    code, frame, _ = _stage(box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box))
    assert code == 1 and f"from {MIRROR} does not match" in str(frame["reason"])


def test_provisional_pins_download_nothing(box: Sandbox) -> None:
    """A pin without a recorded digest (what ``pins.json`` holds while provisional) downloads
    nothing; the committed pins carry digests, so the empty one is set here."""
    code, frame, _ = _stage(box, "runtime", prelude=_pin_prelude(""), env=_uv_env(box))
    assert code == 1
    assert "no recorded SHA-256" in str(frame["reason"])
    assert box.calls("curl") == []


def test_an_archive_without_uv_is_unusable(box: Sandbox) -> None:
    archive, digest = _archive(box, name="not-uv")
    box.rule(tool="curl", match=[UPSTREAM], body_file=str(archive))
    code, frame, _ = _stage(box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box))
    assert code == 1 and frame["reason"] == "the uv archive holds no uv binary"


def test_a_uv_that_does_not_run_is_removed(box: Sandbox) -> None:
    archive, digest = _archive(box, script="#!/bin/sh\nexit 1\n")
    box.rule(tool="curl", match=[UPSTREAM], body_file=str(archive))
    code, frame, _ = _stage(box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box))
    assert code == 1 and frame["reason"] == "the pinned uv does not run on this system"
    assert not _uv_entry(box).exists()


def test_python_is_installed_only_when_none_fits(box: Sandbox) -> None:
    archive, digest = _archive(box)
    box.rule(tool="curl", match=[UPSTREAM], body_file=str(archive))
    code, _, out = _stage(
        box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box, FAKE_UV_NEED_INSTALL="1")
    )
    assert code == 0, out
    calls = (box.state / "uv-calls").read_text().splitlines()
    assert "python find --no-project >=3.13,<3.15" in calls
    assert "python install --no-bin 3.14" in calls


def test_an_existing_python_is_accepted(box: Sandbox) -> None:
    archive, digest = _archive(box)
    box.rule(tool="curl", match=[UPSTREAM], body_file=str(archive))
    code, _, out = _stage(box, "runtime", prelude=_pin_prelude(digest), env=_uv_env(box))
    assert code == 0, out
    assert not [c for c in (box.state / "uv-calls").read_text().splitlines() if "install" in c]


# -- libc -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("interpreter", "libc"),
    [
        ("/lib/ld-musl-x86_64.so.1", "musl"),
        ("/lib/ld-musl-aarch64.so.1", "musl"),
        ("/lib64/ld-linux-x86-64.so.2", "glibc"),
        ("/lib/ld-linux-aarch64.so.1", "glibc"),
    ],
)
def test_libc_from_the_elf_interpreter(box: Sandbox, interpreter: str, libc: str) -> None:
    header = box.root / "sh.bin"
    header.write_bytes(elf_header(interpreter))
    assert len(header.read_bytes()) < 512
    found = box.run_bash(f'source "{INSTALL_SH}"; ins_libc "{header}"')
    assert found == libc


def test_a_non_elf_probe_is_skipped(box: Sandbox) -> None:
    script = box.root / "script.sh"
    script.write_text("#!/bin/sh\n# ld-musl-x86_64.so.1 in a comment\n")
    musl = box.root / "musl.bin"
    musl.write_bytes(elf_header("/lib/ld-musl-x86_64.so.1"))
    assert box.run_bash(f'source "{INSTALL_SH}"; ins_libc "{script}" "{musl}"') == "musl"


# -- dependencies ---------------------------------------------------------------------------------


def _place_uv(box: Sandbox) -> None:
    entry = _uv_entry(box)
    entry.mkdir(parents=True)
    (entry / "uv").write_text(FAKE_UV)
    (entry / "uv").chmod(0o755)


def _checkout(box: Sandbox) -> Path:
    checkout = box.indus_home / "indus-agent"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "uv.lock").write_text("version = 1\n")
    return checkout


def _sync_args(box: Sandbox) -> list[str]:
    (line,) = [c for c in (box.state / "uv-calls").read_text().splitlines() if c.startswith("sync")]
    return line.split()


TARGET = "ins_uv_target() { printf linux-x64; }"


def test_dependencies_sync_the_lock_with_the_common_extras(box: Sandbox) -> None:
    _place_uv(box)
    checkout = _checkout(box)
    code, _, out = _stage(box, "dependencies", prelude=TARGET, env=_uv_env(box))
    assert code == 0, out
    args = _sync_args(box)
    assert args[:6] == ["sync", "--directory", str(checkout), "--frozen", "--no-dev", "--python"]
    extras = [args[i + 1] for i, a in enumerate(args) if a == "--extra"]
    assert extras == ["web", "docs", "pty", "dashboard", "acp", "messaging", "browser"]


def _install_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


def test_skip_browser_now_or_saved_earlier(box: Sandbox) -> None:
    _place_uv(box)
    checkout = _checkout(box)
    code, _, out = _stage(box, "dependencies", "--skip-browser", prelude=TARGET, env=_uv_env(box))
    assert code == 0, out
    assert "browser" not in _sync_args(box)
    (box.state / "uv-calls").unlink()
    # An earlier install saved its extras without the browser group (the key `indus update` reads).
    (box.indus_home / "config.yaml").write_text(
        f"updates:\n  installs:\n    {_install_id(checkout)}:\n      extras:\n"
        "        - web\n        - docs\n"
    )
    code, _, out = _stage(box, "dependencies", prelude=TARGET, env=_uv_env(box))
    assert code == 0, out
    assert "browser" not in _sync_args(box)
    assert "left out: browser" in out
    (box.state / "uv-calls").unlink()
    (box.indus_home / "config.yaml").write_text(
        f"updates:\n  installs:\n    {_install_id(checkout)}:\n      extras: [web, browser]\n"
    )
    code, _, out = _stage(
        box, "dependencies", "--skip-computer-use", prelude=TARGET, env=_uv_env(box)
    )
    assert code == 0, out
    assert "browser" in _sync_args(box)
    assert "left out: computer-use" in out


def test_dependencies_need_the_lock(box: Sandbox) -> None:
    _place_uv(box)
    code, frame, _ = _stage(box, "dependencies", prelude=TARGET, env=_uv_env(box))
    assert code == 1 and "has no uv.lock" in str(frame["reason"])


# -- config ---------------------------------------------------------------------------------------


def _package_venv(box: Sandbox) -> Path:
    checkout = _checkout(box)
    venv = Path(os.environ.get("VIRTUAL_ENV") or ROOT / ".venv")
    if not (venv / "bin" / "python").exists():
        pytest.skip("no project virtualenv to stand in for the installed one")
    (checkout / ".venv").symlink_to(venv)
    return checkout


def test_config_seeds_the_home_and_a_private_env_file(box: Sandbox) -> None:
    _package_venv(box)
    box.indus_home.mkdir(parents=True, exist_ok=True)
    env_file = box.indus_home / ".env"
    env_file.write_text("EXAMPLE_NAME=kept\n")
    env_file.chmod(0o644)
    code, _, out = _stage(box, "config")
    assert code == 0, out
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert env_file.read_text() == "EXAMPLE_NAME=kept\n"
    assert (box.indus_home / "config.yaml").read_text().startswith("_config_version:")
    for sub in ("logs", "sessions", "skills", "cron"):
        assert (box.indus_home / sub).is_dir()


def test_config_creates_env_0600_and_saves_the_extras(box: Sandbox) -> None:
    """The installer writes ``updates.installs.<id>.extras`` (the list ``indus update`` syncs) and
    reads it back for later runs; the updater's own reader agrees (one key, both sides)."""
    from indus.cli.update.channels import saved_extras
    from indus.foundation import yamlio

    checkout = _package_venv(box)
    code, _, out = _stage(box, "config", "--skip-browser", "--skip-computer-use")
    assert code == 0, out
    assert (box.indus_home / ".env").stat().st_mode & 0o777 == 0o600
    saved = box.run_bash(
        f'source "{INSTALL_SH}"; ins_parse_args --indus-home "{box.indus_home}"; '
        f'ins_install_setting "{checkout}" extras'
    )
    assert saved == "web docs pty dashboard acp messaging"
    config = yamlio.load_safe((box.indus_home / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    installs = config["updates"]["installs"]
    assert saved_extras(installs, _install_id(checkout)) == saved.split()


# -- products and PATH ----------------------------------------------------------------------------


def _fake_venv(box: Sandbox) -> Path:
    checkout = _checkout(box)
    program = checkout / ".venv" / "bin" / "indus"
    program.parent.mkdir(parents=True)
    program.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$INDUS_FAKE_TOOLS_STATE/indus-calls"\n')
    program.chmod(0o755)
    return program


def _block_count(path: Path) -> int:
    return path.read_text().splitlines().count("# >>> indus >>>")


def test_path_block_added_once_across_two_runs(box: Sandbox) -> None:
    program = _fake_venv(box)
    (box.home / ".bashrc").write_text("# my bashrc\nexport PATH=$HOME/.local/bin:$PATH\n")
    (box.home / ".bash_profile").write_text("source ~/.bashrc\n")
    for _ in range(2):
        code, _, out = _stage(box, "products")
        assert code == 0, out
    for name in (".bashrc", ".profile", ".bash_profile"):
        assert _block_count(box.home / name) == 1, name
    assert not (box.home / ".bash_login").exists()
    text = (box.home / ".bashrc").read_text()
    assert text.startswith("# my bashrc\nexport PATH=$HOME/.local/bin:$PATH\n\n# >>> indus >>>\n")
    assert text.rstrip().endswith("# <<< indus <<<")
    launcher = (box.home / ".local" / "bin" / "indus").read_text()
    assert f'exec "{program}" "$@"' in launcher
    assert "INDUS_HOME" not in launcher  # the default home needs no export
    assert os.access(box.home / ".local" / "bin" / "indus", os.X_OK)


def test_zsh_and_fish_rc_files(box: Sandbox) -> None:
    _fake_venv(box)
    code, _, out = _stage(box, "products", env=box.env(SHELL="/usr/bin/zsh"))
    assert code == 0, out
    assert _block_count(box.home / ".zshrc") == 1 and _block_count(box.home / ".zprofile") == 1
    assert not (box.home / ".bashrc").exists()
    code, _, out = _stage(box, "products", env=box.env(SHELL="/usr/local/bin/fish"))
    assert code == 0, out
    fish = (box.home / ".config" / "fish" / "config.fish").read_text()
    assert (
        'fish_add_path "$HOME/.local/bin"' in fish
        and _block_count(box.home / ".config" / "fish" / "config.fish") == 1
    )


def test_launcher_for_a_custom_home_and_a_foreign_launcher(box: Sandbox) -> None:
    custom = box.root / "custom-home"
    box.indus_home = custom
    _fake_venv(box)
    code, _, out = _stage(box, "products")
    assert code == 0, out
    launcher = box.home / ".local" / "bin" / "indus"
    assert f'INDUS_HOME="${{INDUS_HOME:-{custom}}}"' in launcher.read_text()
    launcher.write_text("#!/bin/sh\necho someone else's\n")
    code, _, out = _stage(box, "products")
    assert code == 0 and "was not written by this installer" in out
    assert launcher.read_text() == "#!/bin/sh\necho someone else's\n"


def test_products_need_the_environment(box: Sandbox) -> None:
    code, frame, _ = _stage(box, "products")
    assert code == 1 and "run the dependencies stage first" in str(frame["reason"])


# -- setup, gateway, complete ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "command"), [("setup", "indus setup"), ("gateway", "indus gateway install")]
)
def test_without_a_terminal_setup_and_gateway_skip(box: Sandbox, stage: str, command: str) -> None:
    _fake_venv(box)
    code, frame, _ = _stage(box, stage)  # start_new_session: no controlling terminal
    assert code == 0
    assert frame == {
        "ok": True,
        "stage": stage,
        "skipped": True,
        "reason": f"no terminal; run '{command}' later",
    }
    assert not (box.state / "indus-calls").exists()


def test_complete_writes_the_marker_and_saves_an_explicit_channel(box: Sandbox) -> None:
    make_origin(box)
    checkout = box.indus_home / "indus-agent"
    box.git("clone", "-q", OFFICIAL_URL, str(checkout))
    program = checkout / ".venv" / "bin" / "indus"
    program.parent.mkdir(parents=True)
    program.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$INDUS_FAKE_TOOLS_STATE/indus-calls"\n')
    program.chmod(0o755)
    code, _, out = _stage(box, "complete", "--channel", "main")
    assert code == 0, out
    marker = json.loads((checkout / ".indus-bootstrap.json").read_text())
    assert set(marker) == {"schema", "commit", "branch", "channel", "completed_at"}
    assert marker["schema"] == 1 and marker["branch"] == "main" and marker["channel"] == "main"
    assert marker["commit"] == box.git("rev-parse", "HEAD", cwd=checkout)
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", marker["completed_at"])
    assert not list(checkout.glob(".indus-bootstrap.json.*"))
    calls = (box.state / "indus-calls").read_text().splitlines()
    assert calls == [f"config set --force updates.installs.{_install_id(checkout)}.channel main"]
    # Our checkout ignores the marker, so a later run's autostash never sweeps it up.
    assert "/.indus-bootstrap.json" in (ROOT / ".gitignore").read_text().splitlines()


# -- the whole ladder -----------------------------------------------------------------------------


def test_a_full_non_interactive_install_ends_with_a_working_command(box: Sandbox) -> None:
    """Every real stage in order (ACC-S1-1 locally): clone the newest release, pinned uv through
    the mirror, a dependency sync (fake uv lending this test run's environment), the real home
    seeding, the launcher, PATH, the marker, one success receipt; then the launcher runs."""
    venv = Path(os.environ.get("VIRTUAL_ENV") or ROOT / ".venv")
    if not (venv / "bin" / "indus").exists():
        pytest.skip("no project virtualenv to lend to the fake dependency sync")
    bare = make_origin(box, tags=("v0.1.0", "v0.2.0"))
    archive, digest = _archive(box)
    box.rule(tool="curl", match=[UPSTREAM], exit=28)
    box.rule(tool="curl", match=[MIRROR], body_file=str(archive))
    result = box.run(
        "--non-interactive",
        prelude=_pin_prelude(digest),
        env=_uv_env(box, FAKE_UV_VENV=str(venv)),
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    checkout = box.indus_home / "indus-agent"
    assert box.git("rev-parse", "HEAD", cwd=checkout) == _tag_commit(box, bare, "v0.2.0")
    marker = json.loads((checkout / ".indus-bootstrap.json").read_text())
    assert marker["channel"] == "stable" and marker["schema"] == 1
    (receipt,) = [
        json.loads(p.read_text())
        for p in (box.indus_home / "logs" / "install_receipts").glob("*.json")
    ]
    assert receipt["outcome"] == "success"
    assert (box.indus_home / "config.yaml").exists()
    assert _block_count(box.home / ".bashrc") == 1
    launched = subprocess.run(
        [str(box.home / ".local" / "bin" / "indus"), "--version"],
        env=box.env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout.splitlines()[0] == "Indus Agent 0.0.0"


def _tag_commit(box: Sandbox, bare: Path, tag: str) -> str:
    return box.git("rev-parse", f"{tag}^{{commit}}", cwd=bare)
