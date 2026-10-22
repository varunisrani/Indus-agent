"""P25-23: the post-release smoke scripts and the rollback sheet, checked without any network.

What the scripts do against a real install is pinned by the release lane
(``tests/e2e/upgrade/test_smoke_steps.py``); here: syntax, shellcheck and PowerShell parsing when
those tools are installed, the release base they default to, the dry-run plan (which must not
fetch or install anything), argument errors, and that the rollback sheet names every alias the
publish controller moves.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from indus.foundation.versions import RELEASE_IMAGE, RELEASE_REPOSITORY, SANDBOX_IMAGE
from scripts.release.claims import ALIASES, IMAGES

ROOT = Path(__file__).resolve().parents[3]
SMOKE = ROOT / "scripts" / "release" / "smoke"
SHELL_SCRIPTS = ("linux.sh", "macos.sh")
WINDOWS = SMOKE / "windows.ps1"
ROLLBACK = ROOT / "docs" / "internals" / "rollback.md"
BASH = shutil.which("bash")
SHARED_STEPS = [
    "install", "version", "setup", "turn", "approval", "resume", "doctor", "dashboard-gate",
    "dashboard-login", "desktop-download", "licences",
]  # fmt: skip
STEPS = {
    "linux.sh": [*SHARED_STEPS, "desktop-launch", "uninstall"],
    "macos.sh": [*SHARED_STEPS, "signature", "desktop-launch", "uninstall"],
    "windows.ps1": [*SHARED_STEPS, "signature", "desktop-launch", "uninstall"],
}
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def _tripwires(folder: Path) -> Path:
    """A PATH folder whose curl, git, hdiutil and python3 only record that they were called."""
    log = folder / "called.txt"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("curl", "git", "hdiutil", "python3", "uv"):
        tool = folder / name
        tool.write_text(f'#!/bin/sh\necho {name} "$@" >>"{log}"\nexit 99\n', encoding="utf-8")
        tool.chmod(0o755)
    return log


def run_smoke(script: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    bin_dir = tmp_path / "bin"
    _tripwires(bin_dir)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path / "user"),
        "LANG": "C.UTF-8",
    }
    argv = [BASH, str(SMOKE / script), *args]
    # No stdin: a script that waited for a person would hang the test instead of failing it.
    return subprocess.run(
        argv,
        input="",
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )


@needs_bash
@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_shell_scripts_parse(script: str) -> None:
    assert BASH is not None
    done = subprocess.run([BASH, "-n", str(SMOKE / script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_shell_scripts_pass_shellcheck(script: str) -> None:
    done = subprocess.run(
        ["shellcheck", "--shell=bash", "--severity=style", str(SMOKE / script)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not installed")
def test_windows_script_parses() -> None:
    parser = "[System.Management.Automation.Language.Parser]"
    command = (
        "$errors = $null; "
        f"{parser}::ParseFile('{WINDOWS}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    done = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_windows_script_is_ascii_for_windows_powershell() -> None:
    # Windows PowerShell 5.1 reads a file without a BOM in the ANSI code page.
    WINDOWS.read_text(encoding="ascii")


@pytest.mark.parametrize("script", [*SHELL_SCRIPTS, "windows.ps1"])
def test_every_script_defaults_to_the_release_base(script: str) -> None:
    text = (SMOKE / script).read_text(encoding="utf-8")
    found = re.findall(
        r"^(?:SMOKE_DEFAULT_REPOSITORY=|\$SmokeDefaultRepository = )'([^']+)'$", text, re.MULTILINE
    )
    assert found == [RELEASE_REPOSITORY]


@pytest.mark.parametrize("script", [*SHELL_SCRIPTS, "windows.ps1"])
def test_scripts_fetch_only_release_downloads(script: str) -> None:
    text = (SMOKE / script).read_text(encoding="utf-8")
    urls = re.findall(r"https?://[^\s'\"`)]+", text)
    assert urls, "no URL at all"
    loopback = ("http://127.0.0.1", "http://127\\.0\\.0\\.1")  # also as a sed/regex pattern
    for url in urls:
        assert url.startswith(("https://github.com/$", *loopback)), url


@pytest.mark.parametrize("script", [*SHELL_SCRIPTS, "windows.ps1"])
def test_the_step_lists_match(script: str) -> None:
    text = (SMOKE / script).read_text(encoding="utf-8")
    if script.endswith(".sh"):
        block = re.search(r"^SMOKE_STEPS=\(([^)]*)\)", text, re.MULTILINE)
        assert block is not None
        steps = block.group(1).split()
    else:
        block = re.search(r"^\$SmokeSteps = @\(([^)]*)\)", text, re.MULTILINE)
        assert block is not None
        steps = re.findall(r"'([a-z-]+)'", block.group(1))
    assert steps == STEPS[script]


@needs_bash
@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_dry_run_prints_the_plan_and_fetches_nothing(script: str, tmp_path: Path) -> None:
    done = run_smoke(script, tmp_path, "--dry-run", "--tag", "v1.2.3")
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "bin" / "called.txt").exists()
    assert not (tmp_path / "user").exists()
    base = f"https://github.com/{RELEASE_REPOSITORY}/releases/download/v1.2.3"
    assert f"installer:  {base}/install.sh" in done.stdout
    assert f"{base}/Indus-1.2.3-" in done.stdout
    assert "(stable channel)" in done.stdout
    listed = re.findall(r"^\s+\d+\. (\S+)", done.stdout, re.MULTILINE)
    assert listed == STEPS[script]
    assert "Dry run: nothing was fetched or installed." in done.stdout


@needs_bash
def test_a_canary_tag_follows_the_canary_channel(tmp_path: Path) -> None:
    tag = "v1.2.4-canary.20261020T120000Z"
    done = run_smoke(
        "linux.sh", tmp_path, "--dry-run", "--tag", tag, "--repository", "me/indus-fork"
    )
    assert done.returncode == 0, done.stderr
    assert f"{tag} (canary channel) of me/indus-fork" in done.stdout
    assert f"https://github.com/me/indus-fork/releases/download/{tag}/install.sh" in done.stdout


@needs_bash
@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--tag", "1.2.3"),
        ("--tag", "v1.2"),
        ("--tag", "v1.2.3-rc.1"),
        ("--tag", "v1.2.3", "--repository", "no-slash"),
        ("--tag", "v1.2.3", "--channel", "main"),
        ("--tag", "v1.2.3", "--bogus"),
        ("--tag",),
    ],
    ids=["no-tag", "no-v", "short", "rc", "repository", "channel", "unknown", "no-value"],
)
def test_usage_errors_exit_2_before_anything_runs(args: tuple[str, ...], tmp_path: Path) -> None:
    done = run_smoke("linux.sh", tmp_path, *args)
    assert done.returncode == 2, done.stdout + done.stderr
    assert not (tmp_path / "bin" / "called.txt").exists()


@needs_bash
def test_a_workdir_that_holds_files_is_refused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep.txt").write_text("mine\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    log = _tripwires(bin_dir)
    # Only the tool check may look for curl/git: let them answer.
    for name in ("curl", "git"):
        (bin_dir / name).write_text(
            f'#!/bin/sh\necho {name} "$@" >>"{log}"\nexit 0\n', encoding="utf-8"
        )
    done = run_smoke("linux.sh", tmp_path, "--tag", "v1.2.3", "--workdir", str(work))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "--workdir must be a new or empty folder" in done.stderr
    assert (work / "keep.txt").read_text(encoding="utf-8") == "mine\n"
    assert not log.exists()


def test_rollback_sheet_names_every_alias_the_controller_moves() -> None:
    text = ROLLBACK.read_text(encoding="utf-8")
    words = [word for kind in ALIASES.values() for word in kind]
    for image in IMAGES:
        for word in words:
            assert f"`{image.tag_for(word)}`" in text, (image.name, word)
    for repository in (RELEASE_IMAGE, SANDBOX_IMAGE):
        assert repository.replace(RELEASE_REPOSITORY, "<repo>") in text
    for never in (
        "never** reuse a version number",
        "never** delete, move or re-point a tag",
        "fix forward",
    ):
        assert never.lower() in text.lower(), never
    for script in (*SHELL_SCRIPTS, "windows.ps1"):
        assert f"scripts/release/smoke/{script}" in text or script in text
    for step in STEPS["macos.sh"]:
        assert f"| `{step}` |" in text, step
