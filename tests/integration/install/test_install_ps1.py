"""P23-06: ``install.ps1`` core and stages.

There is no Windows (and usually no PowerShell) on the CI host, so the built script is checked
statically here, and the behaviour tests run under ``pwsh`` only when it is installed (they skip
cleanly otherwise). The stage contract is the one P20-09's desktop runner parses
(``apps/desktop/electron/installer/stageProtocol.ts``); ``_parse_manifest`` and ``_parse_frame``
below read output the way that runner does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.integration.install import ps_static
from tests.integration.install.build_script import BUILT_PS1, PS_DIR, ROOT, STAGES_JSON, render_ps1

PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell (pwsh) is not installed here")

LADDER = [
    "prerequisites",
    "repository",
    "runtime",
    "dependencies",
    "config",
    "products",
    "setup",
    "gateway",
    "complete",
]
PARAMETERS = {
    "Branch",
    "Commit",
    "Channel",
    "IndusHome",
    "InstallDir",
    "Manifest",
    "Stage",
    "Json",
    "NonInteractive",
    "SkipSetup",
    "IncludeDesktop",
    "SkipBrowser",
    "SkipComputerUse",
    "ShowResolvedPaths",
    "PosixArgs",
}
FAILURE_CLASSES = {
    "unsupported_platform",
    "download_failed",
    "download_digest_mismatch",
    "uv_unusable",
    "git_extract_failed",
    "python_install_failed",
    "deps_install_failed",
    "products_build_failed",
    "filesystem_error",
    "config_failed",
    "dir_not_checkout",
    "git_clone_failed",
    "git_fetch_failed",
    "git_checkout_failed",
    "git_reset_failed",
    "local_changes_blocked",
    "commit_not_on_branch",
    "setup_failed",
    "gateway_failed",
    "interrupted",
    "other",
}
STAGE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _built() -> str:
    return BUILT_PS1.read_text(encoding="utf-8")


def _stages() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(STAGES_JSON.read_text(encoding="utf-8"))
    return data


def _manifest_from_table(table: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": table["protocol_version"],
        "stages": [
            {
                "name": s["name"],
                "title": s["title"],
                "category": s["category"],
                "needs_user_input": s["needs_user_input"],
            }
            for s in table["stages"]
        ],
    }


def _json_lines(stdout: str) -> list[Any]:
    found: list[Any] = []
    for raw in re.split(r"\r?\n", stdout):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def _parse_manifest(stdout: str) -> list[dict[str, Any]]:
    """What the desktop runner accepts (stageProtocol.parseManifest), or an AssertionError."""
    candidates = [
        v for v in _json_lines(stdout) if isinstance(v, dict) and isinstance(v.get("stages"), list)
    ]
    assert candidates, "the installer printed no manifest"
    last = candidates[-1]
    assert last.get("protocol_version") == 1
    stages: list[dict[str, Any]] = []
    for raw in last["stages"]:
        assert isinstance(raw, dict) and isinstance(raw.get("name"), str)
        assert STAGE_NAME.fullmatch(raw["name"]), raw
        stages.append(raw)
    assert stages
    return stages


def _parse_frame(stdout: str) -> dict[str, Any] | None:
    frames = [
        v
        for v in _json_lines(stdout)
        if isinstance(v, dict) and isinstance(v.get("ok"), bool) and isinstance(v.get("stage"), str)
    ]
    return frames[-1] if frames else None


# -- static: the built file -----------------------------------------------------------------------


def test_built_script_is_fresh_from_its_fragments() -> None:
    assert _built() == render_ps1(), "rebuild: python scripts/install/build.py"


def test_built_script_and_fragments_are_ascii_without_bom() -> None:
    for path in [BUILT_PS1, *sorted(PS_DIR.glob("*.ps1"))]:
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), path
        bad = [n for n, line in enumerate(data.splitlines(), 1) if any(b > 127 for b in line)]
        assert bad == [], f"{path.name}: non-ASCII on lines {bad[:5]} (5.1 reads it as ANSI)"


def test_param_block_is_the_first_statement_and_names_every_parameter() -> None:
    code = ps_static.lex(_built()).code
    first = next(line.strip() for line in code.splitlines() if line.strip())
    assert first.startswith("[CmdletBinding(PositionalBinding = $false)]")
    block = code[code.index("param(") : code.index("\n)\n")]
    declared = set(re.findall(r"\$([A-Za-z]+)\s*(?:=|,|\n|$)", block))
    assert declared == PARAMETERS
    assert "ValueFromRemainingArguments = $true" in block


def test_embedded_tables_equal_their_json_files() -> None:
    here = ps_static.lex(_built()).here_strings
    stages = [json.loads(h) for h in here if '"protocol_version"' in h and '"stages"' in h]
    assert stages == [_stages()]
    pins = [json.loads(h) for h in here if '"git_for_windows"' in h]
    assert len(pins) == 1 and "uv" in pins[0]


def test_brackets_balance_outside_strings_and_comments() -> None:
    code = ps_static.lex(_built()).code
    for opener, closer in ("()", "{}", "[]"):
        depth = 0
        for ch in code:
            depth += (ch == opener) - (ch == closer)
            assert depth >= 0, f"unbalanced {closer}"
        assert depth == 0, f"unbalanced {opener}"


def test_every_called_installer_function_is_defined() -> None:
    code = ps_static.lex(_built()).code
    missing = ps_static.verb_noun_calls(code) - ps_static.function_names(code)
    assert missing == set()


def test_every_script_variable_read_is_assigned() -> None:
    text = _built()
    assert ps_static.script_vars_read(text) - ps_static.script_vars_set(text) == set()


def test_no_powershell_7_only_syntax() -> None:
    code = ps_static.lex(_built()).code
    patterns = (r"\?\?", r"\?\.", r"&&", r"\|\|", r"-AsHashtable", r"-Parallel", r"\$IsWindows")
    for pattern in patterns:
        assert re.search(pattern, code) is None, pattern
    assert re.search(r"-AdditionalChildPath", code) is None


def test_no_assignment_to_automatic_variables() -> None:
    code = ps_static.lex(_built()).code
    automatic = r"\$(args|input|home|host|pid|error|this|matches|profile)\s*=(?!=)"
    assert re.findall(automatic, code, re.I) == []


def test_double_quoted_strings_have_no_scope_qualifier_hazard() -> None:
    """``"$name: x"`` reads a variable from a drive named ``name``; ``${name}:`` is meant."""
    allowed = {"script", "global", "env", "local", "private", "using"}
    for text in ps_static.lex(_built()).double_quoted:
        for match in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*):", text):
            assert match.group(1).lower() in allowed, text


def test_every_failure_class_is_a_known_token() -> None:
    text = _built()
    used = set(re.findall(r"""Stop-IndusStage\s+(?:"[^"]*"|'[^']*')\s+'([a-z_]+)'""", text))
    used |= set(re.findall(r"Write-IndusReceipt\s+'failed'\s+\S+\s+'([a-z_]+)'", text))
    assert used and used <= FAILURE_CLASSES, used - FAILURE_CLASSES


def test_top_level_runs_nothing_but_the_guarded_entry() -> None:
    """Dot-sourcing defines functions only: at depth 0 there are definitions and assignments, and
    the one call is ``Invoke-IndusEntry`` inside ``if (-not $script:IndusDotSourced)``."""
    code = ps_static.lex(_built()).code
    depth = 0
    top: list[str] = []
    guarded = False
    for line in code.splitlines():
        stripped = line.strip()
        if depth == 0 and stripped:
            top.append(stripped)
        if stripped.startswith("if (-not $script:IndusDotSourced)"):
            guarded = True
        depth += line.count("{") - line.count("}")
    allowed = re.compile(
        r"^(function \S|\$script:\w+ = |\[|param\(|\)$"
        r"|\}|if \(-not \$script:IndusDotSourced\) \{$|'@|\$script:IndusOpts = @\{)"
    )
    stray = [line for line in top if not allowed.match(line) and not line.startswith("$")]
    assert stray == [], stray
    assert guarded
    tail = code[code.rindex("if (-not $script:IndusDotSourced)") :]
    assert "Invoke-IndusEntry | Out-Null" in tail
    assert code.count("Invoke-IndusEntry | Out-Null") == 1


def test_receipt_is_first_writer_wins_and_the_finally_records_interrupted() -> None:
    text = _built()
    start, stop = (
        text.index("function Write-IndusReceipt"),
        text.index("function Invoke-IndusNative"),
    )
    body = text[start:stop]
    written_flag = body.index("if ($script:IndusReceiptWritten) { return $false }")
    assert written_flag < body.index("WriteAllText")
    assert "if (Test-Path -LiteralPath $final) { return $false }" in body
    assert "Move-Item -LiteralPath $tmpFile -Destination $final -ErrorAction Stop" in body
    assert "-Force" not in body.split("Move-Item", 1)[1].split("\n", 1)[0]
    assert "installer = 'ps1'" in body
    assert "'install_receipts'" in body
    start, stop = (
        text.index("function Invoke-IndusLadder"),
        text.index("function Invoke-IndusEntry"),
    )
    ladder = text[start:stop]
    finally_part = ladder[ladder.index("} finally {") :]
    assert "Write-IndusReceipt 'failed' $script:IndusCurrentStage 'interrupted'" in finally_part
    assert ladder.index("Write-IndusReceipt 'success'") < ladder.index("} catch {")


def test_machine_output_bypasses_the_pipeline() -> None:
    text = _built()
    for name in ("Write-IndusFrame", "Get-IndusManifestJson"):
        assert f"function {name}" in text
    writer = text[text.index("function Write-IndusJsonLine") :].split("\n}\n", 1)[0]
    assert "[Console]::Out.WriteLine" in writer
    assert "Write-Output" not in ps_static.lex(text).code


# -- the shared stage table -----------------------------------------------------------------------


def test_stage_table_is_the_shared_ladder() -> None:
    table = _stages()
    assert table["protocol_version"] == 1
    assert [s["name"] for s in table["stages"]] == LADDER
    for stage in table["stages"]:
        keys = set(stage) - {"title_with_desktop"}
        assert keys == {"name", "title", "category", "needs_user_input"}
        assert STAGE_NAME.fullmatch(stage["name"])
        assert stage["category"] in {"system", "runtime", "configuration"}
        assert stage["needs_user_input"] is (stage["name"] in {"setup", "gateway"})
    assert table["dispatch_only"] == ["desktop"]
    assert table["extras"]["unless_skip_browser"] == ["browser"]


def test_manifest_from_the_table_passes_the_desktop_parser() -> None:
    manifest = _manifest_from_table(_stages())
    stdout = "Indus Agent installer\n" + json.dumps(manifest, separators=(",", ":")) + "\n"
    assert [s["name"] for s in _parse_manifest(stdout)] == LADDER


def test_manifest_equals_the_shell_scripts(tmp_path: Path) -> None:
    """ACC-S2-2: when ``install.sh`` (P23-03) is present, its manifest is the same table."""
    script = ROOT / "scripts" / "install.sh"
    bash = shutil.which("bash")
    if not script.is_file() or bash is None:
        pytest.skip("scripts/install.sh is built by slice I1; compared at integration")
    env = {**os.environ, "HOME": str(tmp_path), "INDUS_HOME": str(tmp_path / "home")}
    home = str(tmp_path / "home")
    done = subprocess.run(
        [bash, str(script), "--manifest", "--non-interactive", "--indus-home", home],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert _parse_manifest(done.stdout) == _manifest_from_table(_stages())["stages"]


# -- static: parity with install.sh (wave 47, f) -------------------------------------------------

SH_RUNTIME = ROOT / "scripts" / "install" / "sh" / "20-runtime.sh"


def _ps_function(name: str) -> str:
    """The body of one function of the built script (to its closing brace at column 0)."""
    text = _built()
    found = re.search(rf"^function {re.escape(name)}\b.*?^}}$", text, re.MULTILINE | re.DOTALL)
    assert found is not None, f"{name} is not defined"
    return found.group(0)


def test_products_fetch_the_release_pages_like_install_sh() -> None:
    products = _ps_function("Invoke-IndusProducts")
    assert "Install-IndusWebPages" in products
    pages = _ps_function("Install-IndusWebPages")
    # A developer's own build is kept; otherwise the environment's python fetches the pages.
    assert re.search(r"'dist'\)\s+'index\.html'", pages)
    assert "Get-IndusVenvPython" in pages
    fetch = re.search(r"Invoke-IndusLogged '[^']+' \{ (?P<cmd>[^}]*) \} -MayFail", pages)
    assert fetch is not None, "the fetch is logged and never fails the stage"
    assert re.fullmatch(
        r"& \$pgPython -m indus\.web\.bundle fetch --repo \$pgDir --target \$pgTarget",
        fetch.group("cmd"),
    )
    assert re.search(r"'src'\)\s+'indus'\)\s+'web'\)\s+'static'", pages)
    # The same three answers as install.sh: 0 in place, 3 not a release, anything else warns.
    assert "-eq 0" in pages and "-eq 3" in pages and "Write-IndusWarn" in pages
    assert "Stop-IndusStage" not in pages
    sh = SH_RUNTIME.read_text(encoding="utf-8")
    assert "-m indus.web.bundle fetch --repo" in sh and "--target" in sh


def test_marker_carries_the_release_version_like_install_sh() -> None:
    sh = SH_RUNTIME.read_text(encoding="utf-8")
    printf = re.search(r"printf '(\{\"schema\"[^']*)'", sh)
    assert printf is not None
    sh_keys = re.findall(r'"([a-z_]+)":', printf.group(1))
    complete = _ps_function("Invoke-IndusComplete")
    record = re.search(r"\$record = \[ordered\]@\{(?P<body>.*?)\n    \}", complete, re.DOTALL)
    assert record is not None
    ps_keys = re.findall(r"^\s+([a-z_]+)\s+=", record.group("body"), re.MULTILINE)
    assert (
        ps_keys == sh_keys == ["schema", "commit", "branch", "channel", "version", "completed_at"]
    )
    # HEAD's newest release tag (stable or canary), without the v; '' off a release.
    assert "tag --points-at HEAD --list 'v*'" in complete
    assert "$script:IndusStableTag" in complete and "$script:IndusCanaryTag" in complete
    assert "Select-IndusNewestTag" in complete and ".Substring(1)" in complete


# -- pwsh: behaviour ------------------------------------------------------------------------------


def _env(tmp_path: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    user = tmp_path / "user"
    user.mkdir(exist_ok=True)
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "SystemRoot", "TERM", "LANG")}
    env = {
        **keep,
        "HOME": str(user),
        "USERPROFILE": str(user),
        "LOCALAPPDATA": str(user / "AppData" / "Local"),
        "TEMP": str(tmp_path / "temp"),
        "TMP": str(tmp_path / "temp"),
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
        "POWERSHELL_UPDATECHECK": "Off",
        "INDUS_INSTALL_CHANNEL": "",
    }
    (tmp_path / "temp").mkdir(exist_ok=True)
    env.update(extra or {})
    return env


def _run(
    tmp_path: Path, *args: str, extra: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    assert PWSH is not None
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(BUILT_PS1), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=_env(tmp_path, extra),
        cwd=tmp_path,
        check=False,
    )


def _dot(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Dot-source the built script, then run ``body`` in the same session."""
    assert PWSH is not None
    script = tmp_path / "probe.ps1"
    script.write_text(f". '{BUILT_PS1}'\n{body}\n", encoding="utf-8")
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=_env(tmp_path),
        cwd=tmp_path,
        check=False,
    )


@needs_pwsh
@pytest.mark.parametrize(
    "args",
    [
        ("-Manifest",),
        ("--manifest", "--non-interactive", "--indus-home", "HOME"),
        ("-Manifest", "-NonInteractive", "-IndusHome", "HOME"),
    ],
)
def test_pwsh_manifest_in_both_spellings(tmp_path: Path, args: tuple[str, ...]) -> None:
    argv = [str(tmp_path / "h") if a == "HOME" else a for a in args]
    done = _run(tmp_path, *argv)
    assert done.returncode == 0, done.stderr
    assert _parse_manifest(done.stdout) == _manifest_from_table(_stages())["stages"]
    assert not (tmp_path / "h").exists(), "the manifest must not create the home"


@needs_pwsh
@pytest.mark.parametrize("name", ["setup", "gateway"])
def test_pwsh_input_stages_skip_with_a_frame_when_non_interactive(
    tmp_path: Path, name: str
) -> None:
    home = tmp_path / "h"
    done = _run(tmp_path, "--stage", name, "--json", "--non-interactive", "--indus-home", str(home))
    assert done.returncode == 0, done.stderr
    expected = {"ok": True, "stage": name, "skipped": True, "reason": "needs user input"}
    assert _parse_frame(done.stdout) == expected


@needs_pwsh
def test_pwsh_stage_outside_windows_fails_with_a_frame(tmp_path: Path) -> None:
    done = _run(tmp_path, "-Stage", "prerequisites", "-Json", "-IndusHome", str(tmp_path / "h"))
    assert done.returncode == 1
    frame = _parse_frame(done.stdout)
    assert frame is not None and frame["ok"] is False and frame["stage"] == "prerequisites"
    assert "install.sh" in frame["reason"]


@needs_pwsh
def test_pwsh_unknown_stage(tmp_path: Path) -> None:
    done = _run(tmp_path, "--stage", "nope", "--json")
    assert done.returncode == 2
    expected = {"ok": False, "stage": "nope", "skipped": False, "reason": "unknown stage: nope"}
    assert _parse_frame(done.stdout) == expected


@needs_pwsh
@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--bogus",), 1),
        (("--stage",), 2),
        (("--indus-home", "--json"), 2),
        (("--channel", "nightly"), 2),
        (("-Channel", "beta", "-Manifest"), 2),
        (("stray",), 1),
    ],
)
def test_pwsh_usage_errors(tmp_path: Path, args: tuple[str, ...], code: int) -> None:
    done = _run(tmp_path, *args)
    assert done.returncode == code, (done.stdout, done.stderr)


@needs_pwsh
def test_pwsh_channel_from_env_is_checked(tmp_path: Path) -> None:
    done = _run(tmp_path, "--manifest", extra={"INDUS_INSTALL_CHANNEL": "weekly"})
    assert done.returncode == 2


@needs_pwsh
def test_pwsh_show_resolved_paths(tmp_path: Path) -> None:
    home = tmp_path / "h"
    done = _run(tmp_path, "-ShowResolvedPaths", "-IndusHome", str(home))
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert set(report) == {
        "long_profile_root",
        "normalized",
        "resolver",
        "temp",
        "indus_home",
        "install_dir",
    }
    assert report["indus_home"] == str(home)
    assert report["install_dir"] == os.path.join(str(home), "indus-agent")
    assert report["resolver"] == "none"
    assert not home.exists()


@needs_pwsh
def test_pwsh_dot_sourcing_runs_nothing(tmp_path: Path) -> None:
    done = _dot(
        tmp_path,
        "$found = Get-Command Invoke-IndusLadder -ErrorAction SilentlyContinue\n"
        "Write-Output ('defined=' + [bool]$found)",
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines() == ["defined=True"]
    assert not any((tmp_path / "user").rglob("*")), "dot-sourcing wrote files"


@needs_pwsh
def test_pwsh_receipt_is_written_once(tmp_path: Path) -> None:
    home = tmp_path / "h"
    body = f"""
$script:IndusPaths = @{{ Home = '{home}'; InstallDir = '{tmp_path / "code"}' }}
$script:IndusStartedAt = 'start'
$first = Write-IndusReceipt 'success'
$second = Write-IndusReceipt 'failed' 'repository' 'interrupted'
Write-Output "$first $second"
"""
    done = _dot(tmp_path, body)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True False"
    receipts = list((home / "logs" / "install_receipts").glob("*.json"))
    assert len(receipts) == 1
    record = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert record["installer"] == "ps1" and record["outcome"] == "success"
    assert set(record) == {"id", "installer", "outcome", "started_at", "finished_at"}


@needs_pwsh
def test_pwsh_existing_receipt_file_is_never_overwritten(tmp_path: Path) -> None:
    home = tmp_path / "h"
    folder = home / "logs" / "install_receipts"
    folder.mkdir(parents=True)
    (folder / "fixedid.json").write_text('{"outcome":"success"}', encoding="utf-8")
    body = f"""
$script:IndusPaths = @{{ Home = '{home}'; InstallDir = '{tmp_path / "code"}' }}
$script:IndusRunId = 'fixedid'
Write-Output (Write-IndusReceipt 'failed' 'config' 'other')
"""
    done = _dot(tmp_path, body)
    assert done.stdout.strip() == "False"
    assert (folder / "fixedid.json").read_text(encoding="utf-8") == '{"outcome":"success"}'


@needs_pwsh
def test_pwsh_failed_ladder_records_one_failed_receipt(tmp_path: Path) -> None:
    home = tmp_path / "h"
    body = f"""
$script:IndusPaths = @{{ Home = '{home}'; InstallDir = '{tmp_path / "code"}'; Report = @{{}} }}
function Invoke-IndusStage([string]$Name) {{
    if ($Name -eq 'runtime') {{ Stop-IndusStage 'no uv' 'uv_unusable' }}
}}
Write-Output ('exit=' + (Invoke-IndusLadder))
"""
    done = _dot(tmp_path, body)
    assert "exit=1" in done.stdout
    receipts = list((home / "logs" / "install_receipts").glob("*.json"))
    assert len(receipts) == 1
    record = json.loads(receipts[0].read_text(encoding="utf-8"))
    outcome = (record["outcome"], record["failed_stage"], record["failure_class"])
    assert outcome == ("failed", "runtime", "uv_unusable")


@needs_pwsh
def test_pwsh_home_inside_the_install_dir_is_refused(tmp_path: Path) -> None:
    code = tmp_path / "code"
    home = str(code / "home")
    done = _run(tmp_path, "--stage", "config", "--json", "--dir", str(code), "--indus-home", home)
    assert done.returncode == 1
    frame = _parse_frame(done.stdout)
    assert frame is not None and frame["ok"] is False
    assert not code.exists()
