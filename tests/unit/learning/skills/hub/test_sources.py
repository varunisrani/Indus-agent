"""Hub source adapters (P13-25): MockTransport behind the SSRF-safe transport for each source."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.scope import Scope
from indus.learning.skills.hub.lock import Taps
from indus.learning.skills.hub.sources import (
    FALLBACK_CAP_S,
    GitHubSource,
    HubError,
    HubHttp,
    OfficialSource,
    SkillMeta,
    SourceSet,
    referenced_files,
    search_sources,
    url_skill_name,
)
from tests.fakes.clock import FakeClock
from tests.unit.learning.skills.helpers import make_skill
from tests.unit.learning.skills.hub._web import (
    API,
    FIXTURES,
    RAW,
    SKILLS_SH,
    TREE_SHA,
    FakeWeb,
    clean_scope,
    github_repo,
    well_known_site,
)


@pytest.fixture
def hub_scope(scope: Scope) -> Scope:
    return clean_scope(scope)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _closing(sources: SourceSet, coro: Any) -> Any:
    try:
        return await coro
    finally:
        await sources.aclose()


# -- helpers ------------------------------------------------------------------------------------


def test_referenced_files_only_layout_dirs() -> None:
    text = (
        "See `references/a.md`, [b](templates/b.txt) and scripts/run.sh.\n"
        "Not these: ../secret, notes.md, references/<topic>.md, references/*.md, assets/\n"
        "Nor my-references/x.md."
    )
    assert referenced_files(text) == {"references/a.md", "templates/b.txt", "scripts/run.sh"}


def test_referenced_file_leaving_the_folder_refuses_the_bundle() -> None:
    with pytest.raises(HubError, match="outside the skill folder"):
        referenced_files("Read references/../../etc/passwd first.")


@pytest.mark.parametrize(
    ("frontmatter", "url", "expected"),
    [
        ({"name": "pdf-kit"}, "https://x.example/a/SKILL.md", "pdf-kit"),
        ({"name": "Bad Name"}, "https://x.example/tools/csv-tools/SKILL.md", "csv-tools"),
        ({}, "https://x.example/guides/deploy.md", "deploy"),
        ({}, "https://x.example/SKILL.md", None),
        ({}, "https://x.example/docs/README.md", None),
        ({"name": "skill"}, "https://x.example/Weird_Dir/SKILL.md", None),
    ],
)
def test_url_name_ladder(frontmatter: dict[str, Any], url: str, expected: str | None) -> None:
    assert url_skill_name(frontmatter, url) == expected


# -- url ----------------------------------------------------------------------------------------


def test_url_source_fetches_skill_and_referenced_files_only(hub_scope: Scope) -> None:
    web = FakeWeb()
    base = "https://site.example/skills/csv-tools"
    web.add(f"{base}/SKILL.md", "---\ndescription: CSV work.\n---\n\nRead references/tips.md.\n")
    web.add(f"{base}/references/tips.md", "tips")
    web.add(f"{base}/scripts/other.sh", "never fetched")
    sources = web.sources(hub_scope)
    bundle = run(_closing(sources, sources.url.fetch(f"{base}/SKILL.md")))
    assert bundle is not None
    assert bundle.name == "csv-tools" and bundle.trust == "community" and not bundle.awaiting_name
    assert set(bundle.files) == {"SKILL.md", "references/tips.md"}
    assert all("other.sh" not in hit for hit in web.hits)
    # every request went through the pinned, vetted address with the real Host header
    assert all(h.startswith("https://site.example/") for h in web.hits)


def test_url_source_without_a_usable_name_waits_for_one(hub_scope: Scope) -> None:
    web = FakeWeb()
    web.add("https://site.example/SKILL.md", "---\ndescription: d\n---\n\nbody\n")
    sources = web.sources(hub_scope)
    bundle = run(_closing(sources, sources.url.fetch("https://site.example/SKILL.md")))
    assert bundle is not None and bundle.awaiting_name and bundle.name == ""


def test_url_to_a_private_address_is_refused_by_policy(hub_scope: Scope) -> None:
    web = FakeWeb()
    web.add("http://10.0.0.5/SKILL.md", "---\nname: x\ndescription: d\n---\n\nb\n")
    http = HubHttp(hub_scope, inner_factory=lambda _p: httpx.MockTransport(web.handler))
    sources = web.sources(hub_scope)
    sources.url.http = http
    bundle = run(_closing(sources, sources.url.fetch("http://10.0.0.5/SKILL.md")))
    assert bundle is None and web.hits == []
    run(http.aclose())


# -- GitHub -------------------------------------------------------------------------------------


def _github(web: FakeWeb, scope: Scope, **kwargs: Any) -> GitHubSource:
    return GitHubSource(web.http(scope), api=API, raw=RAW, **kwargs)


def test_github_tap_lists_skill_dirs_and_skips_dot_and_underscore(hub_scope: Scope) -> None:
    web = FakeWeb()
    github_repo(web)
    gh = _github(web, hub_scope, taps=[{"repo": "acme/agent-skills", "path": "skills/"}])
    rows = run(gh.list_tap("acme/agent-skills", "skills/"))
    run(gh.http.aclose())
    assert [r.name for r in rows] == ["notes-tool", "sql-helper"]
    sql = next(r for r in rows if r.name == "sql-helper")
    assert sql.identifier == "acme/agent-skills/skills/sql-helper"
    assert sql.category == "data"  # first skills.sh.json grouping wins
    assert sql.tags == ("sql", "databases") and sql.trust == "community"
    api_calls = [h for h in web.hits if h.startswith(API)]
    assert len(api_calls) == 2  # repo + tree, cached for the run


def test_github_fetch_copies_only_referenced_files(hub_scope: Scope) -> None:
    web = FakeWeb()
    github_repo(web)
    gh = _github(web, hub_scope)
    bundle = run(gh.fetch("acme/agent-skills/skills/sql-helper"))
    run(gh.http.aclose())
    assert bundle is not None
    assert set(bundle.files) == {"SKILL.md", "references/dialects.md"}
    assert bundle.metadata["revision"] == TREE_SHA
    assert not any("unused.sh" in h or "README" in h for h in web.hits)
    assert len([h for h in web.hits if h.startswith(API)]) == 2


def test_github_symlinked_reference_is_refused(hub_scope: Scope) -> None:
    web = FakeWeb()
    github_repo(web)
    tree = json.loads((FIXTURES / "github_tree.json").read_text())
    for item in tree["tree"]:
        if item["path"].endswith("dialects.md"):
            item["mode"] = "120000"
    web.add(f"{API}/repos/acme/agent-skills/git/trees/main", tree)
    gh = _github(web, hub_scope)
    with pytest.raises(HubError, match="symlink"):
        run(gh.fetch("acme/agent-skills/skills/sql-helper"))
    run(gh.http.aclose())


def test_private_repo_without_token_says_so(hub_scope: Scope) -> None:
    web = FakeWeb()
    gh = _github(web, hub_scope)
    with pytest.raises(HubError, match="private: set GITHUB_TOKEN"):
        run(gh.fetch("acme/secret-skills/skills/x"))
    run(gh.http.aclose())


def test_private_repo_with_token_sends_it(hub_scope: Scope) -> None:
    web = FakeWeb()
    seen: list[str] = []

    def repo(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"default_branch": "main"})

    github_repo(web, "acme/secret-skills")
    web.add(f"{API}/repos/acme/secret-skills", repo)
    token = "tok" + "-" + "fake-123"
    gh = _github(web, hub_scope, token=token)
    bundle = run(gh.fetch("acme/secret-skills/skills/notes-tool"))
    run(gh.http.aclose())
    assert bundle is not None and seen == [f"Bearer {token}"]


def test_rate_limit_is_a_clear_error(hub_scope: Scope) -> None:
    web = FakeWeb()
    web.add(
        f"{API}/repos/acme/agent-skills",
        lambda _r: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}),
    )
    gh = _github(web, hub_scope)
    with pytest.raises(HubError, match="rate limit.*GITHUB_TOKEN"):
        run(gh.tree("acme/agent-skills"))
    run(gh.http.aclose())


def test_trust_only_for_the_fixed_set(hub_scope: Scope) -> None:
    web = FakeWeb()
    taps = [{"repo": "acme/agent-skills", "path": "skills/", "trust": "trusted"}]
    plain = _github(web, hub_scope, taps=taps)
    assert plain.trust_for("acme/agent-skills") == "community"  # the built-in set is empty
    chosen = _github(web, hub_scope, taps=taps, trusted=frozenset({"acme/agent-skills"}))
    assert chosen.trust_for("acme/agent-skills") == "trusted"
    run(plain.http.aclose())
    run(chosen.http.aclose())


# -- well-known and skills.sh -------------------------------------------------------------------


def test_well_known_index_parsed_and_fetched(hub_scope: Scope) -> None:
    web = FakeWeb()
    base = well_known_site(web)
    sources = web.sources(hub_scope)
    rows = run(sources.well_known.search("https://docs.example.com", 10))
    assert [r.name for r in rows] == ["pdf-tools", "notes"]
    assert rows[0].identifier == f"well-known:{base}/pdf-tools"
    bundle = run(_closing(sources, sources.well_known.fetch(rows[0].identifier)))
    assert bundle is not None and set(bundle.files) == {"SKILL.md", "references/usage.md"}


def test_well_known_unsafe_file_refused(hub_scope: Scope) -> None:
    web = FakeWeb()
    base = "https://evil.example/.well-known/skills"
    web.add(f"{base}/index.json", {"skills": [{"name": "x", "files": ["SKILL.md", "../../x"]}]})
    web.add(f"{base}/x/SKILL.md", "---\nname: x\ndescription: d\n---\n\nb\n")
    sources = web.sources(hub_scope)
    with pytest.raises(HubError, match="unsafe file"):
        run(_closing(sources, sources.well_known.fetch(f"well-known:{base}/x")))


def test_skills_sh_search_and_fetch_through_github(hub_scope: Scope) -> None:
    web = FakeWeb()
    github_repo(web)
    web.add_file(f"{SKILLS_SH}/api/search", FIXTURES / "skills_sh_search.json")
    sources = web.sources(hub_scope)
    rows = run(sources.skills_sh.search("sql", 10))
    assert [r.identifier for r in rows] == [
        "skills-sh/acme/agent-skills/sql-helper",
        "skills-sh/acme/agent-skills/notes-tool",
    ]
    bundle = run(_closing(sources, sources.skills_sh.fetch(rows[0].identifier)))
    assert bundle is not None
    assert bundle.source == "skills-sh" and bundle.identifier == rows[0].identifier
    assert bundle.name == "sql-helper" and bundle.category == "data"
    assert set(bundle.files) == {"SKILL.md", "references/dialects.md"}


# -- official -----------------------------------------------------------------------------------


def test_official_empty_catalogue_works(tmp_path: Path) -> None:
    empty = OfficialSource(tmp_path / "missing")
    assert run(empty.search("", 10)) == []
    assert run(empty.fetch("official/anything")) is None


def test_official_catalogue_is_builtin_trust(tmp_path: Path) -> None:
    make_skill(tmp_path / "cat", "media/gif-maker", description="Make GIFs.")
    source = OfficialSource(tmp_path / "cat")
    rows = run(source.search("gif", 10))
    assert [(r.identifier, r.trust, r.category) for r in rows] == [
        ("official/media/gif-maker", "builtin", "media")
    ]
    bundle = run(source.fetch("official/media/gif-maker"))
    assert bundle is not None and bundle.trust == "builtin"
    assert run(source.fetch("official/../escape")) is None


# -- search -------------------------------------------------------------------------------------


class _Fixed:
    def __init__(self, sid: str, rows: list[SkillMeta], before: Any = None) -> None:
        self.id = sid
        self.rows = rows
        self.before = before
        self.cancelled = False

    def handles(self, identifier: str) -> bool:
        return False

    async def search(self, query: str, limit: int) -> list[SkillMeta]:
        try:
            if self.before is not None:
                await self.before()
            return self.rows
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def resolve(self, identifier: str) -> None:
        return None

    async def fetch(self, identifier: str) -> None:
        return None


def _meta(name: str, source: str) -> SkillMeta:
    return SkillMeta(name=name, description="", source=source, identifier=f"{source}/{name}")


def _set(web: FakeWeb, scope: Scope, **parts: Any) -> SourceSet:
    sources = web.sources(scope)
    # The P24-45 fallback sources answer at once here unless a test sets them.
    for key, sid in (("clawhub", "clawhub"), ("lobehub", "lobehub"), ("browse_sh", "browse-sh")):
        setattr(sources, key, _Fixed(sid, []))
    for key, value in parts.items():
        setattr(sources, key, value)
    return sources


def test_search_fallback_stops_at_eight_seconds(hub_scope: Scope) -> None:
    clock = FakeClock(hub_scope.clock.now())
    web = FakeWeb()

    async def stall() -> None:
        clock.advance(FALLBACK_CAP_S + 1)
        await asyncio.Event().wait()

    async def quick() -> None:
        clock.advance(FALLBACK_CAP_S - 1)

    slow = _Fixed("skills-sh", [_meta("late", "skills-sh")], before=stall)
    sources = _set(web, hub_scope, skills_sh=slow)
    start = clock.monotonic()
    outcome = run(_closing(sources, search_sources(sources, "sql", clock=clock)))
    assert outcome.fallback_used and outcome.timed_out == ["skills-sh"] and slow.cancelled
    assert outcome.rows == []
    assert clock.monotonic() - start == pytest.approx(FALLBACK_CAP_S + 1)

    in_time = _Fixed("skills-sh", [_meta("sql-helper", "skills-sh")], before=quick)
    sources = _set(web, hub_scope, skills_sh=in_time)
    outcome = run(_closing(sources, search_sources(sources, "sql", clock=clock)))
    assert [r.name for r in outcome.rows] == ["sql-helper"] and outcome.timed_out == []


def test_configured_results_and_filters_skip_the_fallback(hub_scope: Scope) -> None:
    web = FakeWeb()
    github_repo(web)
    Taps(hub_scope.home / "skills").add("acme/agent-skills")
    fallback = _Fixed("skills-sh", [_meta("x", "skills-sh")])
    sources = _set(web, hub_scope, skills_sh=fallback)
    sources.github.taps = Taps(hub_scope.home / "skills").list()
    sources.github.api, sources.github.raw = API, RAW
    found = run(search_sources(sources, "sql", clock=hub_scope.clock))
    assert [r.name for r in found.rows] == ["sql-helper"] and not found.fallback_used
    nothing = run(search_sources(sources, "zzz", source_filter="acme/agent-skills"))
    assert nothing.rows == [] and not nothing.fallback_used
    nothing = run(search_sources(sources, "zzz", source_filter="github"))
    assert not nothing.fallback_used
    run(sources.aclose())


def test_search_dedupes_by_identifier_preferring_higher_trust(hub_scope: Scope) -> None:
    web = FakeWeb()
    low = SkillMeta("a", "", "github", "same/id", trust="community")
    high = SkillMeta("a", "", "official", "same/id", trust="builtin")
    sources = _set(
        web, hub_scope, official=_Fixed("official", [high]), github=_Fixed("github", [low])
    )
    outcome = run(_closing(sources, search_sources(sources, "a")))
    assert [(r.identifier, r.trust) for r in outcome.rows] == [("same/id", "builtin")]
