"""Licence check over the TypeScript workspace: root, packages/* and apps/* manifests."""

from __future__ import annotations

import json
from pathlib import Path

from tooling.checks import licenses
from tooling.checks.common import Context, Result
from tooling.checks.tests.conftest import write


def manifest(path: Path, deps: dict[str, str], dev: dict[str, str] | None = None) -> None:
    write(path, json.dumps({"dependencies": deps, "devDependencies": dev or {}}))


def installed(root: Path, name: str, license: str, deps: dict[str, str] | None = None) -> None:
    data = {"name": name, "license": license, "dependencies": deps or {}}
    write(root / "node_modules" / name / "package.json", json.dumps(data))


def check(root: Path) -> Result:
    return licenses.run(Context(root=root))


def test_gpl_runtime_dep_in_package_fails(tmp_path: Path):
    manifest(tmp_path / "packages" / "x" / "package.json", {"copyleft": "1.0.0"})
    installed(tmp_path, "copyleft", "GPL-3.0-only")
    result = check(tmp_path)
    assert result.code == 1
    assert result.violations == [
        "npm:copyleft: licence 'GPL-3.0-only' is not in the allow-list (add an exception + reason)"
    ]


def test_gpl_dev_dependency_is_only_noted(tmp_path: Path):
    manifest(tmp_path / "packages" / "x" / "package.json", {"ok": "1.0.0"}, {"copyleft": "1.0.0"})
    installed(tmp_path, "ok", "MIT")
    installed(tmp_path, "copyleft", "GPL-3.0-only")
    result = check(tmp_path)
    assert result.code == 0, result.violations
    assert any("1 Node devDependencies reported only" in n for n in result.notes)


def test_root_manifest_is_read(tmp_path: Path):
    manifest(tmp_path / "package.json", {"copyleft": "1.0.0"})
    installed(tmp_path, "copyleft", "AGPL-3.0-only")
    assert check(tmp_path).code == 1


def test_transitive_dep_from_package_is_followed(tmp_path: Path):
    manifest(tmp_path / "packages" / "ui" / "package.json", {"top": "1.0.0"})
    installed(tmp_path, "top", "MIT", {"deep": "2.0.0"})
    installed(tmp_path, "deep", "GPL-2.0-only")
    result = check(tmp_path)
    assert result.code == 1 and "npm:deep" in result.violations[0]


def test_workspace_siblings_are_not_licence_checked(tmp_path: Path):
    manifest(tmp_path / "packages" / "ui" / "package.json", {"@indus/protocol": "workspace:*"})
    installed(tmp_path, "@indus/protocol", "UNLICENSED")
    result = check(tmp_path)
    assert result.code == 0, result.violations


def test_not_installed_is_skipped_with_note(tmp_path: Path):
    manifest(tmp_path / "packages" / "ui" / "package.json", {"top": "1.0.0"})
    result = check(tmp_path)
    assert result.code == 0 and any("not installed" in n for n in result.notes)
