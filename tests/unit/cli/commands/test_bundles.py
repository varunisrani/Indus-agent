"""P09-10: `indus bundles` list / show / create / delete / reload, run in process (no terminal)."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.foundation import yamlio
from indus.foundation.scope import Scope
from indus.learning.skills.bundles import bundles_dir, scan_bundles
from tests.unit.cli.commands.cmdrun import run_cli


def write_skill(home: Path, name: str) -> None:
    folder = home / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: about {name}\n---\n\nBody.\n", encoding="utf-8"
    )


def test_non_tty_create_with_flags_then_list_and_show(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    write_skill(scope.home, "brew-tea")
    ran = run_cli(
        capsys,
        scope,
        "bundles",
        "create",
        "Morning Routine",
        "--skill",
        "brew-tea",
        "-s",
        "stretch",
        "--description",
        "Start the day",
        "--instruction",
        "Go in order.",
    )
    assert ran.code == 0, ran.err
    assert "Saved /morning-routine" in ran.out
    assert "Not installed yet (skipped when the bundle runs): stretch" in ran.out
    saved = yamlio.load_safe(bundles_dir(scope.home) / "morning-routine.yaml")
    assert saved["skills"] == ["brew-tea", "stretch"] and saved["instruction"] == "Go in order."

    listed = run_cli(capsys, scope, "bundles", "list")
    assert listed.code == 0
    assert "/morning-routine" in listed.out and "brew-tea, stretch" in listed.out
    assert "Start the day" in listed.out

    shown = run_cli(capsys, scope, "bundles", "show", "morning-routine")
    assert shown.code == 0
    assert "- brew-tea (ok)" in shown.out and "- stretch (missing)" in shown.out


def test_create_refuses_overwrite_without_force(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    assert run_cli(capsys, scope, "bundles", "create", "ops", "--skill", "a").code == 0
    again = run_cli(capsys, scope, "bundles", "create", "ops", "--skill", "b")
    assert again.code == 1 and "--force" in again.err
    assert scan_bundles(scope.home)["ops"].skills == ("a",)
    forced = run_cli(capsys, scope, "bundles", "create", "ops", "--skill", "b", "--force")
    assert forced.code == 0
    assert scan_bundles(scope.home)["ops"].skills == ("b",)


def test_create_without_skills_needs_a_terminal(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = run_cli(capsys, scope, "bundles", "create", "ops")
    assert ran.code == 2 and "--skill" in ran.err
    assert not bundles_dir(scope.home).exists()


def test_delete_needs_yes_without_a_terminal(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    run_cli(capsys, scope, "bundles", "create", "ops", "--skill", "a")
    refused = run_cli(capsys, scope, "bundles", "delete", "ops")
    assert refused.code != 0 and "ops" in scan_bundles(scope.home)
    done = run_cli(capsys, scope, "bundles", "delete", "ops", "-y")
    assert done.code == 0 and scan_bundles(scope.home) == {}
    missing = run_cli(capsys, scope, "bundles", "delete", "ops", "-y")
    assert missing.code == 1 and "No bundle named" in missing.err


def test_list_empty_show_unknown_and_reload(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    empty = run_cli(capsys, scope, "bundles", "list")
    assert empty.code == 0 and "No skill bundles yet" in empty.out
    unknown = run_cli(capsys, scope, "bundles", "show", "nope")
    assert unknown.code == 1 and "No bundle named" in unknown.err
    folder = bundles_dir(scope.home)
    folder.mkdir(parents=True)
    (folder / "late.yaml").write_text("skills: [x]\n", encoding="utf-8")
    reloaded = run_cli(capsys, scope, "bundles", "reload")
    assert reloaded.code == 0 and "1 bundle in" in reloaded.out
