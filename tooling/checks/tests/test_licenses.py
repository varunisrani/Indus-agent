"""Licence check: SPDX evaluation, metadata reading, pyproject and node_modules walking."""

from __future__ import annotations

import importlib.metadata as md
import json
from email.message import Message
from pathlib import Path
from typing import cast

import pytest

from tooling.checks import licenses
from tooling.checks.common import Context
from tooling.checks.tests.conftest import write

ALLOW, _, _ = licenses.load_allow()


@pytest.mark.parametrize(
    ("expr", "ok"),
    [
        ("MIT", True),
        ("Apache-2.0 OR GPL-3.0-only", True),
        ("MIT AND GPL-3.0-only", False),
        ("(MIT OR GPL-2.0) AND BSD-3-Clause", True),
        ("Apache-2.0 WITH LLVM-exception", True),
        ("GPL-3.0-or-later", False),
        ("MIT License", None),
        ("", None),
    ],
)
def test_spdx(expr: str, ok: bool | None):
    assert licenses.spdx_allowed(expr, ALLOW) is ok


def meta(**fields: str | list[str]) -> md.PackageMetadata:
    """Distribution metadata built from header fields (an email Message has the same interface)."""
    m = Message()
    for key, value in fields.items():
        for v in value if isinstance(value, list) else [value]:
            m[key.replace("_", "-")] = v
    return cast(md.PackageMetadata, m)


def test_license_sources_in_order():
    assert licenses.license_of(meta(License_Expression="MIT"), ALLOW) == ("MIT", True)
    assert licenses.license_of(meta(License="BSD-3-Clause"), ALLOW)[1]
    classified = meta(Classifier=["License :: OSI Approved :: MIT License"], License="see file")
    assert licenses.license_of(classified, ALLOW) == ("MIT", True)
    gpl = meta(Classifier=["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"])
    assert licenses.license_of(gpl, ALLOW)[1] is False
    text = meta(License="Permission is hereby granted, free of charge, to any person ...")
    assert licenses.license_of(text, ALLOW) == ("MIT", True)
    assert licenses.license_of(meta(), ALLOW) == ("UNKNOWN", False)


def test_no_pyproject_or_no_runtime_deps_pass(tmp_path: Path):
    assert licenses.run(Context(root=tmp_path)).code == 0
    write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "indus"\ndependencies = []\n\n'
        '[dependency-groups]\ndev = ["pytest==9.1.1", "ruff==0.16.10"]\n',
    )
    result = licenses.run(Context(root=tmp_path))
    assert result.code == 0 and "no runtime" in " ".join(result.notes)


def test_installed_dependency_tree_is_checked(tmp_path: Path):
    write(tmp_path / "pyproject.toml", '[project]\nname = "indus"\ndependencies = ["pytest>=8"]\n')
    result = licenses.run(Context(root=tmp_path))
    names = {d["name"] for d in result.data["dependencies"]}
    assert {"pytest", "pluggy"} <= names
    assert result.code == 0, result.violations


def test_uninstalled_runtime_dependency_is_setup_problem(tmp_path: Path):
    write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "indus"\ndependencies = ["zz-not-installed-pkg>=1"]\n',
    )
    assert licenses.run(Context(root=tmp_path)).code == 2


def test_node_modules_production_deps(tmp_path: Path):
    write(
        tmp_path / "apps" / "web" / "package.json",
        json.dumps({"dependencies": {"left": "1"}, "devDependencies": {"devonly": "1"}}),
    )
    nm = tmp_path / "apps" / "web" / "node_modules"
    write(
        nm / "left" / "package.json", json.dumps({"license": "MIT", "dependencies": {"deep": "1"}})
    )
    write(tmp_path / "node_modules" / "deep" / "package.json", json.dumps({"license": "GPL-3.0"}))
    write(nm / "devonly" / "package.json", json.dumps({"license": "GPL-3.0"}))
    allow = tmp_path / "allow.json"
    base = json.loads(licenses.ALLOW_FILE.read_text(encoding="utf-8"))
    write(allow, json.dumps(base))
    result = licenses.run(Context(root=tmp_path), allow)
    assert result.code == 1
    assert len(result.violations) == 1 and "npm:deep" in result.violations[0]
    base["exceptions"] = {"deep": {"license": "GPL-3.0", "reason": "build tool only"}}
    write(allow, json.dumps(base))
    assert licenses.run(Context(root=tmp_path), allow).code == 0
    base["exceptions"] = {"deep": {"license": "GPL-3.0"}}
    write(allow, json.dumps(base))
    assert licenses.run(Context(root=tmp_path), allow).code == 2


def test_node_without_install_is_skipped(tmp_path: Path):
    write(tmp_path / "apps" / "web" / "package.json", json.dumps({"dependencies": {"x": "1"}}))
    result = licenses.run(Context(root=tmp_path))
    assert result.code == 0 and any("not installed" in n for n in result.notes)
