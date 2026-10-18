"""The layer contract (import-linter) and the pyright configuration in pyproject.toml."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE = ROOT / "src" / "indus"

# Low to high, as in docs/plan/phases/00-layout-and-conventions.md section 1.
LAYERS: list[list[str]] = [
    ["platform"],
    ["foundation"],
    ["protocol", "state"],
    ["providers", "safety", "pm"],
    ["backends"],
    ["tools"],
    ["core"],
    ["learning", "plugins", "mcp", "cron", "board"],
    ["service"],
    ["cli", "tui_bridge", "gateway", "web", "acp", "mcp_serve", "research"],
]
LAYER_PACKAGES = [name for layer in LAYERS for name in layer if name != "cli"]
STRICT = ["foundation", "protocol", "state", "service", "core"]


def _config() -> dict[str, Any]:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_contract_lists_layers_in_order() -> None:
    linter = _config()["tool"]["importlinter"]
    assert linter["root_package"] == "indus"
    (contract,) = linter["contracts"]
    assert contract["type"] == "layers"
    expected = [" | ".join(f"indus.{n}" for n in layer) for layer in reversed(LAYERS)]
    assert contract["layers"] == expected


def test_layer_packages_exist_with_docstring() -> None:
    assert (PACKAGE / "py.typed").is_file()
    for name in LAYER_PACKAGES:
        text = (PACKAGE / name / "__init__.py").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert len(lines) == 1, name
        assert lines[0].startswith('"""') and lines[0].endswith('"""'), name


def test_pyright_config() -> None:
    cfg = _config()
    assert cfg["project"]["requires-python"] == ">=3.13,<3.15"
    pyright = cfg["tool"]["pyright"]
    assert pyright["pythonVersion"] == "3.13"
    assert pyright["typeCheckingMode"] == "basic"
    assert pyright["include"] == ["src", "tests", "tooling"]
    assert pyright["strict"] == [f"src/indus/{n}" for n in STRICT]
    assert pyright["reportMissingTypeStubs"] is False


def test_single_config_file() -> None:
    for name in ("ruff.toml", ".ruff.toml", "pyrightconfig.json", ".importlinter", "setup.cfg"):
        assert not (ROOT / name).exists(), name


def _skeleton(tmp_path: Path) -> Path:
    """Copy the package and pyproject into tmp_path; return the copied package dir."""
    shutil.copy(PYPROJECT, tmp_path / "pyproject.toml")
    dest = tmp_path / "src" / "indus"
    shutil.copytree(PACKAGE, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _lint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Run import-linter's API on the copy in tmp_path; True when every contract is kept."""
    from importlinter.application import use_cases
    from importlinter.configuration import configure

    configure()
    # The real `indus` is already imported; hide it so the copy is the one found.
    for key in [k for k in sys.modules if k == "indus" or k.startswith("indus.")]:
        monkeypatch.delitem(sys.modules, key)
    monkeypatch.syspath_prepend(str(tmp_path / "src"))
    monkeypatch.chdir(tmp_path)

    options = use_cases.read_user_options(str(tmp_path / "pyproject.toml"))
    use_cases._register_contract_types(options)  # pyright: ignore[reportPrivateUsage]
    report = use_cases.create_report(options, cache_dir=None)
    assert report.could_not_run is False
    return not report.contains_failures


def test_upward_import_breaks_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _skeleton(tmp_path)
    (pkg / "foundation" / "x.py").write_text("import indus.core\n", encoding="utf-8")
    assert _lint(tmp_path, monkeypatch) is False


def test_sibling_import_breaks_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _skeleton(tmp_path)
    (pkg / "protocol" / "x.py").write_text("import indus.state\n", encoding="utf-8")
    assert _lint(tmp_path, monkeypatch) is False


def test_downward_import_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _skeleton(tmp_path)
    (pkg / "core" / "x.py").write_text("import indus.foundation\n", encoding="utf-8")
    (pkg / "foundation" / "y.py").write_text("import indus.platform\n", encoding="utf-8")
    assert _lint(tmp_path, monkeypatch) is True


def _pyright(project: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", "-p", str(project / "pyproject.toml")],
        capture_output=True,
        text=True,
        cwd=project,
        check=False,
        timeout=300,
    )
    return json.loads(proc.stdout)


@pytest.mark.slow
def test_untyped_function_fails_only_under_strict_path(tmp_path: Path) -> None:
    pkg = _skeleton(tmp_path)
    untyped = "def double(value):\n    return value * 2\n"
    (pkg / "foundation" / "probe.py").write_text(untyped, encoding="utf-8")
    (pkg / "tools" / "probe.py").write_text(untyped, encoding="utf-8")

    result = _pyright(tmp_path)
    errors = [d for d in result["generalDiagnostics"] if d["severity"] == "error"]
    files = {Path(d["file"]).relative_to(tmp_path).as_posix() for d in errors}
    assert files == {"src/indus/foundation/probe.py"}
