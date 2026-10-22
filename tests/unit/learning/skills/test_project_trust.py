"""Project skills: trust, scan cache and quarantine (P13-28, F12, R07.26)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.learning.skills import project_trust as pt
from indus.learning.skills.prompt_index import IndexOptions, build_index
from indus.learning.skills.resolver import (
    TIER_PROJECT,
    SkillQuarantined,
    clear_scan_cache,
    load_catalog,
)
from indus.safety import content_scan
from tests.unit.learning.skills.helpers import make_skill, with_skills_config

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "repos" / "project_skills"
DANGEROUS_LINE = "rm -rf " + "/"


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    clear_scan_cache()
    yield
    clear_scan_cache()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The fixture project copied out, with a ``.git`` folder (a repo root)."""
    root = tmp_path / "work" / "project"
    shutil.copytree(FIXTURE, root)
    (root / ".git").mkdir()
    return root


def trusting(scope: Scope, *roots: Path, **more: Any) -> Scope:
    return with_skills_config(scope, trusted_project_dirs=[str(r) for r in roots], **more)


def project_names(scope: Scope, cwd: Path) -> dict[str, str]:
    with pt.bind_project_cwd(cwd):
        catalog = load_catalog(scope)
    return {r.name: r.status for r in catalog.records if r.tier == TIER_PROJECT}


def scans(scope: Scope) -> list[Path]:
    folder = scope.home / pt.SCAN_CACHE_DIR
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


# -- finding the root ---------------------------------------------------------------------------


def test_root_is_the_nearest_git_ancestor(repo: Path) -> None:
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    assert pt.find_project_root(deep) == repo.resolve()
    (repo / "src" / ".git").write_text("gitdir: ../.git/worktrees/x\n")  # a worktree's .git file
    assert pt.find_project_root(deep) == (repo / "src").resolve()


def test_walk_stops_after_64_levels(tmp_path: Path) -> None:
    top = tmp_path / "top"
    (top / ".git").mkdir(parents=True)
    near = top.joinpath(*["d"] * (pt.MAX_WALK - 1))
    far = top.joinpath(*["d"] * (pt.MAX_WALK + 1))
    far.mkdir(parents=True)
    assert pt.find_project_root(near) == top.resolve()
    assert pt.find_project_root(far) is None


def test_home_is_never_a_project_root(tmp_path: Path) -> None:
    home = tmp_path / "me"
    (home / ".git").mkdir(parents=True)
    (home / "notes").mkdir()
    assert pt.find_project_root(home / "notes", home=home) is None


# -- trust --------------------------------------------------------------------------------------


def test_untrusted_repo_counts_but_never_indexes(scope: Scope, repo: Path) -> None:
    with pt.bind_project_cwd(repo):
        state = pt.project_skills(scope)
        catalog = load_catalog(scope)
    assert state.root == repo.resolve() and not state.trusted
    assert state.found == 3 and state.pending == 3 and state.quarantined == {}
    assert catalog.settings.project_pending == 3
    assert [r for r in catalog.records if r.tier == TIER_PROJECT] == []
    assert scans(scope) == []  # nothing parsed or scanned before trust


def test_trusted_repo_indexes_with_project_tag_and_quarantines(scope: Scope, repo: Path) -> None:
    trusted = trusting(scope, repo)
    assert project_names(trusted, repo) == {
        "deploy-notes": "unique",
        "lint-rules": "unique",
        "cleanup": "quarantined",
    }
    with pt.bind_project_cwd(repo):
        catalog = load_catalog(trusted)
    index = build_index(catalog, IndexOptions())
    assert "- deploy-notes: [project] How this project is deployed" in index
    assert "- lint-rules: [project] " in index
    assert "cleanup" not in index
    with pytest.raises(SkillQuarantined, match="quarantined: the content scan found dangerous"):
        catalog.resolve("cleanup")
    assert catalog.resolve("deploy-notes").tier == TIER_PROJECT
    # the scan cache lives in the profile, never in the repo
    assert len(scans(trusted)) == 3
    assert not list(repo.rglob("*.json"))


def test_quarantined_skill_hides_nothing_below_it(scope: Scope, repo: Path) -> None:
    make_skill(scope.home / "skills", "cleanup", description="My own cleanup.")
    trusted = trusting(scope, repo)
    with pt.bind_project_cwd(repo):
        catalog = load_catalog(trusted)
    assert catalog.resolve("cleanup").description == "My own cleanup."


def test_editing_a_trusted_skill_rescans(scope: Scope, repo: Path) -> None:
    trusted = trusting(scope, repo)
    assert project_names(trusted, repo)["deploy-notes"] == "unique"
    before = scans(trusted)
    skill = repo / ".indus" / "skills" / "deploy-notes" / "SKILL.md"
    original = skill.read_text()
    skill.write_text(original + f"\n    {DANGEROUS_LINE}\n")
    clear_scan_cache()
    assert project_names(trusted, repo)["deploy-notes"] == "quarantined"
    assert len(scans(trusted)) == len(before) + 1
    skill.write_text(original)
    clear_scan_cache()
    assert project_names(trusted, repo)["deploy-notes"] == "unique"
    assert len(scans(trusted)) == len(before) + 1  # the first report came from the cache


@pytest.mark.parametrize("command", [DANGEROUS_LINE, "for" + "mat c:"])
@pytest.mark.parametrize(
    "wrap",
    [
        "Clean it with `{c}` when stuck.",
        "```sh\n{c}\n```",
        "```sh\n{c}```",
        "1. Then:\n\n   ```sh\n   {c}\n   ```",
    ],
)
def test_a_command_in_a_code_span_or_fence_is_quarantined(
    scope: Scope, repo: Path, wrap: str, command: str
) -> None:
    trusted = trusting(scope, repo)
    skill = repo / ".indus" / "skills" / "deploy-notes" / "SKILL.md"
    skill.write_text(skill.read_text() + "\n" + wrap.format(c=command) + "\n")
    assert project_names(trusted, repo)["deploy-notes"] == "quarantined"


def test_scan_failure_quarantines(
    scope: Scope, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("scanner down")

    # project_trust imports the scanner when it first scans (W48-g-3).
    monkeypatch.setattr(content_scan, "scan_tree", broken)
    names = project_names(trusting(scope, repo), repo)
    assert set(names.values()) == {"quarantined"}


def test_discovery_off_finds_nothing(scope: Scope, repo: Path) -> None:
    off = trusting(scope, repo, project_discovery=False)
    with pt.bind_project_cwd(repo):
        assert pt.project_skills(off) == pt.ProjectSkills()
    assert project_names(off, repo) == {}


def test_profile_skills_dir_is_never_a_project_dir(scope: Scope, tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / ".agents").mkdir()
    (root / ".agents" / "skills").symlink_to(scope.home / "skills", target_is_directory=True)
    make_skill(scope.home / "skills", "mine")
    state = pt.project_skills(trusting(scope, root), root)
    assert state.dirs == () and state.found == 0


def test_agent_writes_never_land_in_the_project(scope: Scope, repo: Path) -> None:
    trusted = trusting(scope, repo)
    with pt.bind_project_cwd(repo):
        settings = load_catalog(trusted).settings
    assert settings.project_dirs and settings.write_root == scope.home / "skills"


# -- surfaces -----------------------------------------------------------------------------------


def test_cron_in_an_untrusted_repo_never_prompts_and_loads_nothing(
    scope: Scope, repo: Path
) -> None:
    def never(_root: Path, _count: int) -> bool:
        raise AssertionError("an unattended surface asked")

    with pt.bind_project_cwd(repo):  # the cron job's workdir
        state = pt.offer_trust(scope, platform="cron", ask=never)
    assert state.pending == 3
    assert project_names(scope, repo) == {}


def test_unattended_surface_inherits_trust(scope: Scope, repo: Path) -> None:
    trusted = trusting(scope, repo)
    with pt.bind_project_cwd(repo):
        state = pt.offer_trust(trusted, platform="api", ask=lambda _r, _c: False)
    assert state.trusted and "deploy-notes" in project_names(trusted, repo)


def test_interactive_offer_trusts_or_remembers_no(scope: Scope, repo: Path) -> None:
    live = scope.with_config(loader.load(scope))
    asked: list[int] = []

    def no(_root: Path, count: int) -> bool:
        asked.append(count)
        return False

    first = pt.offer_trust(live, platform="cli", ask=no, cwd=repo)
    again = pt.offer_trust(live, platform="cli", ask=no, cwd=repo)
    assert asked == [3] and not first.trusted and not again.trusted

    other = repo.parent / "other"
    shutil.copytree(repo, other)
    state = pt.offer_trust(live, platform="cli", ask=lambda _r, _c: True, cwd=other)
    assert state.trusted and len(state.quarantined) == 1
    assert loader.load(live).get("skills.trusted_project_dirs") == [str(other.resolve())]


def test_trust_project_writes_config_once(scope: Scope, repo: Path) -> None:
    live = scope.with_config(loader.load(scope))
    assert pt.trust_project(live, repo / "src") == repo.resolve()
    assert pt.trust_project(live, repo) == repo.resolve()
    assert loader.load(live).get("skills.trusted_project_dirs") == [str(repo.resolve())]
    assert "deploy-notes" in project_names(live, repo)
    with pytest.raises(ValueError, match="not inside a git repository"):
        pt.trust_project(live, repo.parent)
