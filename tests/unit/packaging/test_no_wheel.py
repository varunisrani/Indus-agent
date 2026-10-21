"""No PyPI: wheel and sdist builds refused outside the Nix build (P23-01, ACC-S7-1)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "scripts" / "build_hooks" / "no_wheel.py"


def _hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("indus_no_wheel_hook", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("target", "version", "env", "refused"),
    [
        ("wheel", "standard", {}, True),
        ("sdist", "standard", {}, True),
        ("wheel", "editable", {}, False),
        ("wheel", "standard", {"INDUS_NIX_BUILD": "1"}, False),
        ("sdist", "standard", {"INDUS_NIX_BUILD": "1"}, False),
        ("wheel", "standard", {"INDUS_NIX_BUILD": "true"}, True),
        ("wheel", "standard", {"INDUS_NIX_BUILD": "0"}, True),
        ("wheel", "editable", {"INDUS_NIX_BUILD": "0"}, False),
    ],
)
def test_decision_table(target: str, version: str, env: dict[str, str], refused: bool) -> None:
    assert (_hook().refusal(target, version, env) is not None) is refused


def test_message_points_to_channels() -> None:
    message = _hook().MESSAGE
    for word in ("install.sh", "install.ps1", "Docker", "Nix", "uv sync", "INDUS_NIX_BUILD=1"):
        assert word in message


def test_hook_raises_before_building(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _hook()
    monkeypatch.delenv("INDUS_NIX_BUILD", raising=False)
    hook = module.NoWheelHook.__new__(module.NoWheelHook)
    hook.target_name = "wheel"  # set by hatchling on a real hook
    with pytest.raises(RuntimeError, match="no PyPI"):
        hook.initialize("standard", {})
    hook.initialize("editable", {})
    monkeypatch.setenv("INDUS_NIX_BUILD", "1")
    hook.initialize("standard", {})


def test_pyproject_registers_the_hook() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '[tool.hatch.build.hooks.custom]\npath = "scripts/build_hooks/no_wheel.py"' in text


def _uv_build(tmp_path: Path, *, nix: bool) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    env = {k: v for k, v in os.environ.items() if k != "INDUS_NIX_BUILD"}
    if nix:
        env["INDUS_NIX_BUILD"] = "1"
    env["UV_NO_CONFIG"] = "1"
    return subprocess.run(
        [uv, "build", "--wheel", "--offline", "--no-progress", "--out-dir", str(tmp_path / "out")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.slow
def test_uv_build_wheel_refused_then_allowed(tmp_path: Path) -> None:
    refused = _uv_build(tmp_path, nix=False)
    if refused.returncode != 0 and "no PyPI" not in refused.stderr:
        pytest.skip(f"the build backend is not available offline: {refused.stderr[-300:]}")
    assert refused.returncode != 0
    assert "Indus Agent is not distributed as a wheel" in refused.stderr
    assert not list((tmp_path / "out").glob("*.whl"))
    allowed = _uv_build(tmp_path, nix=True)
    assert allowed.returncode == 0, allowed.stderr[-2000:]
    assert [p.name for p in (tmp_path / "out").glob("*.whl")] == [
        "indus_agent-0.0.0-py3-none-any.whl"
    ]
