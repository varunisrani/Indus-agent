"""Skills and bundles as CLI slash commands (P09-09, P09-10 wiring in ``slash.skill_cmds``)."""

from __future__ import annotations

from pathlib import Path

from indus.cli.slash.context import Extensions
from indus.cli.slash.dispatch import dispatch
from indus.cli.slash.skill_cmds import skill_commands
from indus.foundation.scope import Scope
from indus.learning.skills.bundles import save_bundle
from tests.unit.cli.slash._fakes import make_ctx


def write_skill(home: Path, name: str, body: str) -> None:
    folder = home / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: about {name}\n---\n\n{body}\n", encoding="utf-8"
    )


def test_skills_and_bundles_become_commands(scope: Scope) -> None:
    write_skill(scope.home, "brew-tea", "Boil water first.")
    write_skill(scope.home, "fold-map", "Fold along the creases.")
    save_bundle(scope.home, "morning", ["brew-tea", "gone-skill"], instruction="Start the day.")
    table = skill_commands(scope)
    assert set(table) == {"brew-tea", "fold-map", "morning"}
    ctx = make_ctx(scope, extensions=Extensions(skill_commands=table))

    result = dispatch("/brew-tea /fold-map after lunch", ctx)
    assert result.submit is not None
    text = result.submit
    assert "Boil water first." in text and "Fold along the creases." in text
    assert text.rstrip().endswith("User instruction: after lunch")

    result = dispatch("/morning quickly", ctx)
    assert result.submit is not None
    assert "Bundle instruction: Start the day." in result.submit
    assert "Boil water first." in result.submit and "gone-skill" in result.submit
    assert "Not loaded: gone-skill." in ctx.text


def test_bundle_wins_over_a_same_slug_skill(scope: Scope) -> None:
    write_skill(scope.home, "deploy", "The skill body.")
    write_skill(scope.home, "checks", "Run the checks.")
    save_bundle(scope.home, "deploy", ["checks"])
    ctx = make_ctx(scope, extensions=Extensions(skill_commands=skill_commands(scope)))
    result = dispatch("/deploy", ctx)
    assert result.submit is not None
    assert "Run the checks." in result.submit and "The skill body." not in result.submit


def test_no_skills_no_commands(scope: Scope) -> None:
    assert skill_commands(scope) == {}
