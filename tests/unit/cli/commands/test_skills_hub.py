"""``indus skills`` hub verbs, the deep link and ``/skills search|install`` (P13-27, P13-28)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.cli.commands import skills_hub
from indus.cli.slash.dispatch import dispatch
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.learning.skills.hub.install import Preview
from indus.learning.skills.hub.lock import HubLock, Taps
from indus.learning.skills.hub.sources import SourceSet
from indus.learning.skills.resolver import clear_scan_cache
from indus.learning.skills.view_tools import clear_list_cache
from tests.unit.cli.commands._helpers_d import run
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.learning.skills.hub._web import FakeWeb, clean_scope, github_repo

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
SITE = "https://site.example/skills"
CAUTION_LINE = "chmod -R 777 " + "/etc"


def _skill(name: str, body: str = "Do it.\n") -> str:
    return f"---\nname: {name}\ndescription: The {name} skill.\n---\n\n{body}"


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWeb]:
    fake = FakeWeb()
    github_repo(fake)
    fake.add(f"{SITE}/plain/SKILL.md", _skill("plain"))
    fake.add(f"{SITE}/risky/SKILL.md", _skill("risky", f"Run {CAUTION_LINE} now.\n"))

    def sources(scope: Scope) -> SourceSet:
        made = fake.sources(clean_scope(scope))
        made.github.taps = Taps(scope.home / "skills").list()
        return made

    monkeypatch.setattr(skills_hub, "make_sources", sources)
    clear_scan_cache()
    clear_list_cache()
    yield fake
    clear_scan_cache()


def installed(scope: Scope) -> set[str]:
    return set(HubLock(scope.home / "skills").installed())


def test_tap_add_list_remove_and_search(scope: Scope, web: FakeWeb) -> None:
    assert run(scope, "skills", "tap", "add", "acme/agent-skills").code == 0
    listed = run(scope, "skills", "tap", "list")
    assert "acme/agent-skills  skills/  community" in listed.out
    found = run(scope, "skills", "search", "sql", "--json")
    assert found.code == 0, found.err
    rows = json.loads(found.out)
    assert [(r["name"], r["identifier"], r["category"]) for r in rows] == [
        ("sql-helper", "acme/agent-skills/skills/sql-helper", "data")
    ]
    browse = run(scope, "skills", "browse")
    assert "notes-tool" in browse.out and "sql-helper" in browse.out
    assert run(scope, "skills", "tap", "add", "not-a-repo").code == 2
    assert run(scope, "skills", "tap", "remove", "acme/agent-skills").code == 0
    assert run(scope, "skills", "tap", "remove", "acme/agent-skills").code == 1


def test_trust_is_limited_to_the_fixed_set(scope: Scope, web: FakeWeb) -> None:
    run(scope, "skills", "tap", "add", "acme/agent-skills")
    refused = run(scope, "skills", "trust", "acme/agent-skills")
    assert refused.code == 1 and "cannot be trusted" in refused.err
    assert run(scope, "skills", "untrust", "acme/agent-skills").code == 0


def test_inspect_leaves_nothing_installed(scope: Scope, web: FakeWeb) -> None:
    result = run(scope, "skills", "inspect", f"{SITE}/risky/SKILL.md")
    assert result.code == 0, result.err
    assert "caution" in result.out and "Nothing was installed." in result.out
    assert not (scope.home / "skills" / "risky").exists() and installed(scope) == set()
    quarantine = scope.home / "skills" / ".hub" / "quarantine"
    assert not quarantine.exists() or not any(quarantine.iterdir())


def test_install_needs_a_terminal_or_yes(scope: Scope, web: FakeWeb) -> None:
    url = f"{SITE}/plain/SKILL.md"
    no_tty = run(scope, "skills", "install", url)
    assert no_tty.code == 2 and "-y" in no_tty.err and installed(scope) == set()
    asked = run(scope, "skills", "install", url, tty=True, input="y\n")
    assert asked.code == 0, asked.err
    assert installed(scope) == {"plain"}


def test_non_tty_caution_install_without_force_exits_1(scope: Scope, web: FakeWeb) -> None:
    url = f"{SITE}/risky/SKILL.md"
    blocked = run(scope, "skills", "install", url, "-y")
    assert blocked.code == 1 and "--force" in blocked.err
    forced = run(scope, "skills", "install", url, "-y", "--force")
    assert forced.code == 0, forced.err
    assert installed(scope) == {"risky"}


def test_check_update_audit_uninstall(scope: Scope, web: FakeWeb) -> None:
    assert run(scope, "skills", "install", "acme/agent-skills/skills/notes-tool", "-y").code == 0
    check = run(scope, "skills", "check-updates")
    assert check.code == 0 and "notes-tool" in check.out and "up_to_date" in check.out
    web.add(
        "https://raw.githubusercontent.com/acme/agent-skills/5f1e0c7/skills/notes-tool/SKILL.md",
        _skill("notes-tool", "Second version.\n"),
    )
    assert "update_available" in run(scope, "skills", "check-updates").out
    assert run(scope, "skills", "update").code == 2
    updated = run(scope, "skills", "update", "--all")
    assert updated.code == 0 and "Updated notes-tool." in updated.out
    target = next((scope.home / "skills").rglob("notes-tool/SKILL.md"))
    assert "Second version." in target.read_text()
    audit = run(scope, "skills", "audit")
    assert audit.code == 0 and "safe" in audit.out
    assert run(scope, "skills", "uninstall", "notes-tool").code == 2  # needs -y without a tty
    gone = run(scope, "skills", "uninstall", "notes-tool", "-y")
    assert gone.code == 0 and installed(scope) == set() and not target.exists()
    assert run(scope, "skills", "uninstall", "notes-tool", "-y").code == 1


def test_list_shows_hub_source(scope: Scope, web: FakeWeb) -> None:
    run(scope, "skills", "install", f"{SITE}/plain/SKILL.md", "-y")
    rows = json.loads(run(scope, "skills", "list", "--json").out)
    assert {r["name"]: r["source"] for r in rows}["plain"] == "hub"


def test_deep_link_ignores_profile_and_force(scope: Scope, web: FakeWeb, tmp_path: Path) -> None:
    other = scope.home.parent / "other"
    asked: list[str] = []

    def yes(preview: Preview) -> bool:
        asked.append(preview.name)
        return True

    link = (
        f"indus://skill/install?identifier={SITE}/plain/SKILL.md&profile=other&force=1&skip_scan=1"
    )
    result = skills_hub.deep_link_install(scope, link, confirm_install=yes)
    assert result.ok and asked == ["plain"]
    assert (scope.home / "skills" / "plain").is_dir() and not other.exists()
    risky = skills_hub.deep_link_install(
        scope,
        f"indus://skill/install?identifier={SITE}/risky/SKILL.md&force=true",
        confirm_install=yes,
    )
    assert risky.status == "blocked" and asked == ["plain"]
    bad = skills_hub.deep_link_install(
        scope, "https://x/skill/install?identifier=a", confirm_install=yes
    )
    assert bad.status == "failed"


def test_trust_project_verb(scope: Scope, tmp_path: Path, web: FakeWeb) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "repos" / "project_skills", repo)
    (repo / ".git").mkdir()
    result = run(scope, "skills", "trust-project", str(repo / "src"))
    assert result.code == 0, result.err
    assert f"Trusted {repo.resolve()}: 3 project skills." in result.out
    assert "1 quarantined" in result.out
    view = loader.load(scope)
    assert view.get("skills.trusted_project_dirs") == [str(repo.resolve())]
    outside = run(scope, "skills", "trust-project", str(tmp_path))
    assert outside.code == 1 and "not inside a git repository" in outside.err


def test_chat_search_and_install(scope: Scope, web: FakeWeb) -> None:
    Taps(scope.home / "skills").add("acme/agent-skills")
    answers: list[str] = []
    ctx = make_ctx(scope, confirm=lambda q: answers.append(q) or True)
    dispatch("/skills search sql", ctx)
    assert "acme/agent-skills/skills/sql-helper" in ctx.text
    ctx.lines.clear()
    dispatch(f"/skills install {SITE}/plain/SKILL.md", ctx)
    assert answers and "Installed plain." in ctx.text
    assert "plain" in ctx.extensions.skill_commands
    unattended = make_ctx(scope, interactive=False)
    dispatch(f"/skills install {SITE}/risky/SKILL.md", unattended)
    assert "cannot ask" in unattended.text and "risky" not in installed(scope)


def test_chat_start_offers_trust_once_and_banner_counts(scope: Scope, tmp_path: Path) -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from rich.console import Console

    from indus.cli.launch import project_skills_check
    from indus.cli.repl.banner import BannerInfo, banner

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "repos" / "project_skills", repo)
    (repo / ".git").mkdir()
    live = scope.with_config(loader.load(scope))
    launch = cast(Any, SimpleNamespace(scope=live, cwd=repo))
    asked: list[int] = []

    def decline(_root: Path, count: int) -> bool:
        asked.append(count)
        return False

    assert project_skills_check(launch, decline) == 3
    assert project_skills_check(launch, decline) == 3
    assert asked == [3]  # asked once; the "no" is remembered
    console = Console(width=100, record=True)
    console.print(banner(BannerInfo(version="1", model="m", project_skills_pending=3)))
    assert "3 skills in this repo not loaded (indus skills trust-project)" in console.export_text()
