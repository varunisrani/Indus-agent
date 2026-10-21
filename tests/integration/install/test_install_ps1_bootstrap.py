"""P23-07: the Windows bootstrap of ``install.ps1`` and the ``install.cmd`` shim.

Static checks always run; the fake-download and fake-clock cases run under ``pwsh`` only when it is
installed (they dot-source the built script and replace its seams: ``IndusDownload``,
``IndusStartProcess``, ``IndusKillTree``, ``IndusNow``, ``IndusSleep``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.integration.install import ps_static
from tests.integration.install.build_script import (
    BUILT_PS1,
    CMD_SHIM,
    PS_DIR,
    latest_asset_url,
    repository,
)
from tooling.checks.banned import load_patterns, scan_text

PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell (pwsh) is not installed here")
BOOTSTRAP = PS_DIR / "10-bootstrap.ps1"


def _function(text: str, name: str) -> str:
    start = text.index(f"function {name}")
    end = text.find("\nfunction ", start + 1)
    return text[start : end if end > 0 else len(text)]


# -- static ---------------------------------------------------------------------------------------


def test_bootstrap_fragment_is_in_the_built_script() -> None:
    fragment = BOOTSTRAP.read_text(encoding="utf-8").strip()
    assert fragment in BUILT_PS1.read_text(encoding="utf-8")


def test_mirror_only_after_a_network_class_failure_and_digest_always_checked() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    body = _function(text, "Invoke-IndusVerifiedDownload")
    catch = body[body.index("} catch {") : body.index("continue")]
    assert "if ($kind -ne 'network')" in catch
    assert "'download_failed'" in catch
    after = body[body.index("$digest = Get-IndusFileSha256 $OutFile") :]
    mismatch = after.index("'download_digest_mismatch'")
    assert after.index("if ($digest -eq $Sha256.ToLowerInvariant()) { return }") < mismatch
    assert "continue" not in after[:mismatch], "a bad digest must never fall through to the mirror"
    assert "StartsWith('https://')" in body
    assert "$Sha256 -notmatch '^[0-9a-fA-F]{64}$'" in body, "a pin without a digest is refused"
    kinds = _function(text, "Get-IndusDownloadFailureKind")
    statuses = ("NameResolutionFailure", "ConnectFailure", "Timeout", "ProtocolError")
    for status in (*statuses, "ReceiveFailure"):
        assert f"'{status}'" in kinds
    assert "'TrustFailure', 'SecureChannelFailure'" in kinds
    assert "AuthenticationException" in kinds


def test_git_extract_is_bounded_and_killed_by_pid() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "$script:IndusGitExtractLimitSeconds = 600" in text
    bounded = _function(text, "Invoke-IndusBoundedProcess")
    assert "& $script:IndusKillTree $proc.Id | Out-Null" in bounded
    assert "& $script:IndusNow" in bounded and "& $script:IndusSleep" in bounded
    assert "taskkill.exe /T /F /PID $ProcessId" in text
    code = ps_static.lex(text).code
    assert re.search(r"Stop-Process\s+-Name|taskkill(\.exe)?\s+/IM", code, re.IGNORECASE) is None
    git = _function(text, "Get-IndusGit")
    assert git.index("Invoke-IndusVerifiedDownload") < git.index("Invoke-IndusBoundedProcess")
    assert "'git_extract_failed'" in git
    move = git.index("Move-Item -LiteralPath $unpacked -Destination $entry")
    assert git.index("Invoke-IndusBoundedProcess") < move


def test_tools_live_under_the_home_and_git_never_touches_the_user_path() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "('git-' + $pin.Version)" in _function(text, "Get-IndusGit")
    assert "('uv-' + $pin.Version + '-' + $target)" in _function(text, "Get-IndusUv")
    use = _function(text, "Use-IndusGit")
    assert "$env:Path = $cmdDir + ';' + $env:Path" in use
    assert "SetEnvironmentVariable" not in use and "Registry" not in use


def test_uv_failure_classes() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    uv = _function(text, "Get-IndusUv")
    assert "'unsupported_platform'" in uv and "'uv_unusable'" in uv
    assert "Test-IndusUvRuns $uvExe" in uv


def test_desktop_check_and_start_menu_entry() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    find = _function(text, "Find-IndusDesktopApp")
    assert "'win-unpacked', 'win-arm64-unpacked'" in find and "'Indus.exe'" in find
    desktop = _function(text, "Invoke-IndusDesktop")
    assert "'products_build_failed'" in desktop
    shortcut = _function(text, "New-IndusStartMenuShortcut")
    assert "GetFolderPath('Programs')" in shortcut and "'Indus.lnk'" in shortcut
    assert "} catch {" in shortcut, "a shortcut failure only warns"


def test_no_reference_names_in_the_fragments_or_the_shim() -> None:
    patterns, problems = load_patterns()
    assert not problems
    for path in [*sorted(PS_DIR.glob("*.ps1")), BUILT_PS1, CMD_SHIM]:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert scan_text(text, patterns) == [], path.name


def test_cmd_shim_calls_the_latest_release_installer() -> None:
    data = CMD_SHIM.read_bytes()
    assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n"), "batch files need CRLF"
    assert all(b < 128 for b in data)
    text = data.decode("ascii")
    url = latest_asset_url(repository(), "install.ps1")
    assert f'set "INDUS_INSTALL_PS1={url}"' in text
    urls = set(re.findall(r"https://\S+?(?=[\"'\s])", text))
    assert urls == {url}, "every URL in the shim comes from the one release constant"
    launch = next(line for line in text.splitlines() if line.startswith("powershell "))
    assert "-NoProfile" in launch and "-ExecutionPolicy Bypass" in launch
    assert "%INDUS_INSTALL_PS1%" in launch and "%*" in launch
    assert "exit $LASTEXITCODE" in launch
    lowered = text.lower()
    assert "pause" in lowered and "exit /b 1" in lowered
    assert "goto" not in lowered and ":" + "eof" not in lowered
    failure = text[text.index("if errorlevel 1 (") :]
    assert "irm '%INDUS_INSTALL_PS1%' | iex" in failure


def test_release_constant_is_https_github() -> None:
    assert re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", repository())


# -- pwsh: fakes ----------------------------------------------------------------------------------


def _probe(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    assert PWSH is not None
    user = tmp_path / "user"
    user.mkdir(exist_ok=True)
    (tmp_path / "temp").mkdir(exist_ok=True)
    script = tmp_path / "probe.ps1"
    prologue = f". '{BUILT_PS1}'\n$ErrorActionPreference = 'Stop'\n"
    script.write_text(f"{prologue}{body}\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(user),
        "USERPROFILE": str(user),
        "TEMP": str(tmp_path / "temp"),
        "TMP": str(tmp_path / "temp"),
        "TMPDIR": str(tmp_path / "temp"),
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
    }
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=tmp_path,
        check=False,
    )


def _lines(done: subprocess.CompletedProcess[str]) -> list[str]:
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


@needs_pwsh
def test_pwsh_digest_mismatch_refuses_and_never_tries_the_mirror(tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    body = f"""
$script:IndusDownload = {{
    param($Uri, $OutFile)
    Add-Content -LiteralPath '{calls}' $Uri
    Set-Content -LiteralPath $OutFile 'not the pinned bytes'
}}
try {{
    Invoke-IndusVerifiedDownload -Url 'https://up.example/a' -MirrorUrl 'https://mirror.example/a' `
        -Sha256 ('0' * 64) -OutFile '{tmp_path / "a.bin"}'
    Write-Output 'no-error'
}} catch {{
    Write-Output ('class=' + $script:IndusFailureClass)
}}
Write-Output ('left=' + (Test-Path -LiteralPath '{tmp_path / "a.bin"}'))
"""
    done = _probe(tmp_path, body)
    assert _lines(done) == ["class=download_digest_mismatch", "left=False"], done.stderr
    assert calls.read_text(encoding="utf-8").split() == ["https://up.example/a"]


@needs_pwsh
def test_pwsh_network_failure_uses_the_mirror_and_verifies_it(tmp_path: Path) -> None:
    payload = b"pinned bytes\n"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    calls = tmp_path / "calls.txt"
    body = f"""
$script:IndusDownload = {{
    param($Uri, $OutFile)
    Add-Content -LiteralPath '{calls}' $Uri
    $status = [System.Net.WebExceptionStatus]::ConnectFailure
    if ($Uri -like 'https://up.*') {{ throw (New-Object System.Net.WebException('down', $status)) }}
    Copy-Item -LiteralPath '{source}' -Destination $OutFile
}}
Invoke-IndusVerifiedDownload -Url 'https://up.example/a' -MirrorUrl 'https://mirror.example/a' `
    -Sha256 '{digest}' -OutFile '{tmp_path / "a.bin"}'
Write-Output 'ok'
"""
    done = _probe(tmp_path, body)
    assert _lines(done)[-1] == "ok", done.stderr
    expected = ["https://up.example/a", "https://mirror.example/a"]
    assert calls.read_text(encoding="utf-8").split() == expected


@needs_pwsh
def test_pwsh_tls_failure_never_tries_the_mirror(tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    body = f"""
$script:IndusDownload = {{
    param($Uri, $OutFile)
    Add-Content -LiteralPath '{calls}' $Uri
    $status = [System.Net.WebExceptionStatus]::TrustFailure
    throw (New-Object System.Net.WebException('bad cert', $status))
}}
try {{
    Invoke-IndusVerifiedDownload -Url 'https://up.example/a' -MirrorUrl 'https://mirror.example/a' `
        -Sha256 ('0' * 64) -OutFile '{tmp_path / "a.bin"}'
}}
catch {{ Write-Output ('class=' + $script:IndusFailureClass) }}
"""
    done = _probe(tmp_path, body)
    assert _lines(done) == ["class=download_failed"], done.stderr
    assert calls.read_text(encoding="utf-8").split() == ["https://up.example/a"]


@needs_pwsh
def test_pwsh_missing_digest_is_refused_before_any_download(tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    body = f"""
$script:IndusDownload = {{ param($Uri, $OutFile) Add-Content -LiteralPath '{calls}' $Uri }}
try {{
    Invoke-IndusVerifiedDownload -Url 'https://up.example/a' -MirrorUrl '' -Sha256 '' `
        -OutFile '{tmp_path / "a.bin"}'
}}
catch {{ Write-Output ('class=' + $script:IndusFailureClass) }}
"""
    done = _probe(tmp_path, body)
    assert _lines(done) == ["class=download_failed"], done.stderr
    assert not calls.exists()


@needs_pwsh
def test_pwsh_extract_timeout_kills_the_child_by_pid(tmp_path: Path) -> None:
    body = """
$script:IndusFakeClock = [DateTime]::new(2026, 1, 1)
$script:IndusKilled = @()
$script:IndusStartProcess = {
    param($FilePath, $ArgLine)
    $fake = [pscustomobject]@{ Id = 4242; HasExited = $false; ExitCode = $null }
    $wait = { param($ms) return $true }
    $fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value $wait
    return $fake
}
$script:IndusNow = { $script:IndusFakeClock }
$script:IndusSleep = {
    param($Milliseconds)
    $script:IndusFakeClock = $script:IndusFakeClock.AddSeconds(30)
}
$script:IndusKillTree = { param($ProcessId) $script:IndusKilled += $ProcessId }
$run = Invoke-IndusBoundedProcess -FilePath 'extractor.exe' -ArgLine '-y' -LimitSeconds 600
Write-Output ('timedout=' + $run.TimedOut)
Write-Output ('killed=' + ($script:IndusKilled -join ','))
Write-Output ('elapsed=' + ($script:IndusFakeClock - [DateTime]::new(2026, 1, 1)).TotalSeconds)
"""
    done = _probe(tmp_path, body)
    assert _lines(done) == ["timedout=True", "killed=4242", "elapsed=600"], done.stderr


@needs_pwsh
def test_pwsh_git_extract_timeout_fails_with_its_class(tmp_path: Path) -> None:
    payload = b"fake extractor\n"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    target = {"url": "https://up.example/git.exe", "mirror": "", "sha256": digest}
    pins = {
        "git_for_windows": {
            "version": "9.9.9",
            "targets": {"windows-x64": target, "windows-arm64": target},
        },
    }
    home = tmp_path / "h"
    body = f"""
$script:IndusPinsData = ('{json.dumps(pins)}' | ConvertFrom-Json)
$script:IndusPaths = @{{ Home = '{home}'; InstallDir = '{tmp_path / "code"}' }}
$script:IndusDownload = {{
    param($Uri, $OutFile)
    Copy-Item -LiteralPath '{source}' -Destination $OutFile
}}
$script:IndusFakeClock = [DateTime]::new(2026, 1, 1)
$script:IndusStartProcess = {{
    param($FilePath, $ArgLine)
    $fake = [pscustomobject]@{{ Id = 7; HasExited = $false; ExitCode = $null }}
    $wait = {{ param($ms) return $true }}
    $fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value $wait
    return $fake
}}
$script:IndusNow = {{ $script:IndusFakeClock }}
$script:IndusSleep = {{
    param($Milliseconds)
    $script:IndusFakeClock = $script:IndusFakeClock.AddSeconds(100)
}}
$script:IndusKillTree = {{ param($ProcessId) Write-Output "kill $ProcessId" }}
try {{ Get-IndusGit | Out-Null; Write-Output 'no-error' }}
catch {{ Write-Output ('class=' + $script:IndusFailureClass) }}
Write-Output ('installed=' + (Test-Path -LiteralPath '{home / "tools"}'))
"""
    done = _probe(tmp_path, body)
    lines = _lines(done)
    assert "class=git_extract_failed" in lines, (done.stdout, done.stderr)
    assert "installed=False" in lines


@needs_pwsh
def test_pwsh_no_pin_for_the_machine_is_unsupported(tmp_path: Path) -> None:
    body = f"""
$script:IndusPinsData = ('{{"uv": {{"version": "1", "targets": {{}}}}}}' | ConvertFrom-Json)
$script:IndusPaths = @{{ Home = '{tmp_path / "h"}'; InstallDir = '{tmp_path / "code"}' }}
try {{ Get-IndusUv | Out-Null }} catch {{ Write-Output ('class=' + $script:IndusFailureClass) }}
"""
    done = _probe(tmp_path, body)
    assert _lines(done) == ["class=unsupported_platform"], done.stderr
