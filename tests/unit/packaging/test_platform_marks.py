"""The ``platforms`` host marker and the bare-skipif lint (P23-01, R10.21)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.platform_marks import check_spec, host_matches, host_name

ROOT = Path(__file__).resolve().parents[3]
BASELINE = Path(__file__).with_name("platform_skip_baseline.json")


@pytest.mark.parametrize(
    ("specs", "host", "expected"),
    [
        (["linux"], "linux", True),
        (["linux"], "macos", False),
        (["linux", "macos"], "macos", True),
        (["posix"], "linux", True),
        (["posix"], "macos", True),
        (["posix"], "windows", False),
        (["not windows"], "linux", True),
        (["not windows"], "windows", False),
        (["not posix"], "windows", True),
        (["not linux", "linux"], "linux", True),
        (["windows"], "windows", True),
    ],
)
def test_host_matching(specs: list[str], host: str, expected: bool) -> None:
    assert host_matches(specs, host) is expected


def test_host_names() -> None:
    assert host_name("linux") == "linux"
    assert host_name("darwin") == "macos"
    assert host_name("win32") == "windows"


@pytest.mark.parametrize("spec", ["linux", "not macos", "posix", "windows"])
def test_valid_specs(spec: str) -> None:
    assert check_spec(spec) is None


@pytest.mark.parametrize("spec", ["Linux", "darwin", "not", "not not linux", "", 3])
def test_invalid_specs(spec: object) -> None:
    assert check_spec(spec) is not None


def _child_pytest(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = --strict-markers -p no:cacheprovider\nmarkers =\n"
        "    platforms(*specs): host gating\n",
        encoding="utf-8",
    )
    (tmp_path / "test_child.py").write_text(body, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.platform_marks",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-q",
            str(tmp_path / "test_child.py"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_two_platforms_marks_fail_collection(tmp_path: Path) -> None:
    result = _child_pytest(
        tmp_path,
        "import pytest\n\n"
        "pytestmark = pytest.mark.platforms('linux')\n\n"
        "@pytest.mark.platforms('macos')\n"
        "def test_x():\n    pass\n",
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "2 platforms marks" in result.stdout + result.stderr


def test_a_bad_spec_fails_collection(tmp_path: Path) -> None:
    result = _child_pytest(
        tmp_path,
        "import pytest\n\n@pytest.mark.platforms('darwin')\ndef test_x():\n    pass\n",
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "'darwin' is not one of" in result.stdout + result.stderr


def test_other_hosts_are_deselected(tmp_path: Path) -> None:
    here = host_name()
    other = "windows" if here != "windows" else "linux"
    result = _child_pytest(
        tmp_path,
        "import pytest\n\n"
        f"@pytest.mark.platforms({other!r})\n"
        "def test_elsewhere():\n    raise AssertionError('must not run here')\n\n"
        f"@pytest.mark.platforms({here!r})\n"
        "def test_here():\n    pass\n\n"
        f"@pytest.mark.platforms('not {other}')\n"
        "def test_not_other():\n    pass\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed, 1 deselected" in result.stdout


# -- R10.21 lint: no new bare skipif(sys.platform ...) -------------------------------------------


def _platform_skipifs(path: Path) -> int:
    count = 0
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "skipif":
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Name)
                and (sub.value.id, sub.attr) in (("sys", "platform"), ("platform", "system"))
            ):
                count += 1
                break
    return count


def test_no_new_bare_platform_skipifs() -> None:
    baseline: dict[str, int] = json.loads(BASELINE.read_text(encoding="utf-8"))["files"]
    found: dict[str, int] = {}
    for path in sorted((ROOT / "tests").rglob("*.py")):
        count = _platform_skipifs(path)
        if count:
            found[path.relative_to(ROOT).as_posix()] = count
    new = {f: n for f, n in found.items() if n > baseline.get(f, 0)}
    assert not new, (
        "bare skipif(sys.platform ...) found; use @pytest.mark.platforms(...) instead: "
        f"{sorted(new)}"
    )
    stale = {f: n for f, n in baseline.items() if found.get(f, 0) < n}
    assert not stale, f"platform_skip_baseline.json lists fewer skipifs now; shrink it: {stale}"
