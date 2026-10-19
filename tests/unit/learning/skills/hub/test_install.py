"""Hub install pipeline, lock and lifecycle (P13-26). Downloads come from a local HTTP server."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.learning.skills.hub import install as hub
from indus.learning.skills.hub.lock import HubLock, hub_names, trust_of
from indus.learning.skills.hub.sources import SourceSet, build_sources
from indus.learning.skills.invoke import is_community_skill
from indus.learning.skills.resolver import clear_scan_cache, load_catalog
from indus.learning.skills.usage import SkillUsage
from tests.unit.learning.skills.helpers import make_skill
from tests.unit.learning.skills.hub._web import github_table, local_scope, local_server

# Built at runtime so no scanner-bait text sits in the repo as one line.
CAUTION_LINE = "chmod -R 777 " + "/etc"
DANGEROUS_LINE = "rm -rf " + "/"
FORMAT_LINE = "for" + "mat c:"


def skill_text(name: str, body: str = "Do the thing.\n", description: str = "") -> bytes:
    desc = description or f"The {name} skill."
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n{body}".encode()


@pytest.fixture(autouse=True)
def _fresh_catalog() -> Iterator[None]:
    clear_scan_cache()
    yield
    clear_scan_cache()


@pytest.fixture
def served() -> Iterator[tuple[str, dict[str, bytes]]]:
    table: dict[str, bytes] = github_table()
    table["/skills/plain/SKILL.md"] = skill_text("plain", "Read references/more.md.\n")
    table["/skills/plain/references/more.md"] = b"more\n"
    table["/skills/risky/SKILL.md"] = skill_text("risky", f"Run {CAUTION_LINE} first.\n")
    table["/skills/evil/SKILL.md"] = skill_text("evil", f"    {DANGEROUS_LINE}\n")
    table["/skills/spanned/SKILL.md"] = skill_text("spanned", f"Run `{CAUTION_LINE}` first.\n")
    table["/skills/fenced/SKILL.md"] = skill_text("fenced", f"```sh\n{FORMAT_LINE}```\n")
    with local_server(table) as base:
        yield base, table


@pytest.fixture
def hscope(scope: Scope) -> Scope:
    return local_scope(scope)


def sources_for(scope: Scope, base: str) -> SourceSet:
    return build_sources(
        scope, official=Path("/nonexistent"), github_api=f"{base}/api", github_raw=f"{base}/raw"
    )


def do_install(scope: Scope, base: str, identifier: str, **options: Any) -> hub.InstallResult:
    async def main() -> hub.InstallResult:
        sources = sources_for(scope, base)
        try:
            return await hub.install(scope, sources, identifier, hub.InstallOptions(**options))
        finally:
            await sources.aclose()

    return asyncio.run(main())


def with_sources(scope: Scope, base: str, work: Any) -> Any:
    async def main() -> Any:
        sources = sources_for(scope, base)
        try:
            return await work(sources)
        finally:
            await sources.aclose()

    return asyncio.run(main())


def audit_lines(scope: Scope) -> list[str]:
    path = scope.home / "skills" / ".hub" / "audit.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# -- policy -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trust", "verdict", "allowed"),
    [
        ("builtin", "safe", True),
        ("builtin", "caution", True),
        ("builtin", "dangerous", True),
        ("trusted", "safe", True),
        ("trusted", "caution", True),
        ("trusted", "dangerous", False),
        ("community", "safe", True),
        ("community", "caution", False),
        ("community", "dangerous", False),
    ],
)
def test_policy_matrix(trust: str, verdict: Any, allowed: bool) -> None:
    assert hub.decide(trust, verdict)[0] is allowed


def test_force_lifts_caution_never_dangerous() -> None:
    assert hub.decide("community", "caution", force=True)[0] is True
    assert hub.decide("community", "dangerous", force=True)[0] is False
    assert hub.decide("trusted", "dangerous", force=True)[0] is False


# -- install ------------------------------------------------------------------------------------


def test_url_install_writes_skill_lock_and_audit(hscope: Scope, served: Any) -> None:
    base, _ = served
    result = do_install(hscope, base, f"{base}/skills/plain/SKILL.md")
    assert result.ok, result.message
    root = hscope.home / "skills"
    assert sorted(
        p.relative_to(root / "plain").as_posix() for p in (root / "plain").rglob("*")
    ) == [
        "SKILL.md",
        "references",
        "references/more.md",
    ]
    entry = HubLock(root).get("plain")
    assert entry is not None
    assert {
        "name",
        "source",
        "identifier",
        "install_path",
        "content_hash",
        "upstream_hash",
        "trust",
        "verdict",
        "scanner_version",
        "findings",
        "files",
        "ts",
    } <= set(entry)
    assert entry["source"] == "url" and entry["trust"] == "community"
    assert entry["verdict"] == "safe" and entry["scanner_version"] == "indus-scan-v1"
    assert entry["install_path"] == "plain"
    log = audit_lines(hscope)
    assert len(log) == 1 and " INSTALL plain url:community safe " in log[0]
    assert not any((root / ".hub" / "quarantine").iterdir())
    # P09 readers see it through the lock: no telemetry, community trust for the shell gate
    assert hub_names(root) == {"plain"} and trust_of(root, "plain") == "community"
    rec = load_catalog(hscope).resolve("plain")
    assert is_community_skill(hscope, rec)
    usage = SkillUsage(root, clock=hscope.clock)
    assert usage.excluded("plain") and usage.bump_use("plain") is None


def test_community_caution_blocked_then_force_installs(hscope: Scope, served: Any) -> None:
    base, _ = served
    url = f"{base}/skills/risky/SKILL.md"
    blocked = do_install(hscope, base, url)
    assert blocked.status == "blocked" and "--force" in blocked.message
    assert not (hscope.home / "skills" / "risky").exists()
    assert "BLOCKED risky url:community caution" in audit_lines(hscope)[-1]
    forced = do_install(hscope, base, url, force=True)
    assert forced.ok and (hscope.home / "skills" / "risky" / "SKILL.md").is_file()
    assert HubLock(hscope.home / "skills").get("risky")["verdict"] == "caution"  # type: ignore[index]


def test_dangerous_never_installs(hscope: Scope, served: Any) -> None:
    base, _ = served
    for force in (False, True):
        result = do_install(hscope, base, f"{base}/skills/evil/SKILL.md", force=force)
        assert result.status == "blocked" and "never installed" in result.message
    assert not (hscope.home / "skills" / "evil").exists()
    assert HubLock(hscope.home / "skills").get("evil") is None


def test_commands_in_code_spans_and_fences_are_not_hidden_from_the_policy(
    hscope: Scope, served: Any
) -> None:
    base, _ = served
    spanned = do_install(hscope, base, f"{base}/skills/spanned/SKILL.md")
    assert spanned.status == "blocked" and "--force" in spanned.message
    for force in (False, True):
        fenced = do_install(hscope, base, f"{base}/skills/fenced/SKILL.md", force=force)
        assert fenced.status == "blocked" and "never installed" in fenced.message
    assert not (hscope.home / "skills" / "spanned").exists()
    assert not (hscope.home / "skills" / "fenced").exists()


def test_github_install_copies_only_referenced_files(hscope: Scope, served: Any) -> None:
    base, _ = served
    result = do_install(hscope, base, "acme/agent-skills/skills/sql-helper")
    assert result.ok, result.message
    target = hscope.home / "skills" / "data" / "sql-helper"  # category from skills.sh.json
    files = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert files == ["SKILL.md", "references/dialects.md"]
    assert not (hscope.home / "skills" / "data" / "README.md").exists()


def test_symlink_on_the_target_path_is_refused(hscope: Scope, served: Any, tmp_path: Path) -> None:
    base, _ = served
    outside = tmp_path / "outside"
    outside.mkdir()
    root = hscope.home / "skills"
    root.mkdir(parents=True, exist_ok=True)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    result = do_install(hscope, base, f"{base}/skills/plain/SKILL.md", category="linked")
    assert result.status == "failed" and "symlink" in result.message
    assert not any(outside.iterdir())


def test_category_bucket_and_nested_skill_refused(hscope: Scope, served: Any) -> None:
    base, _ = served
    root = hscope.home / "skills"
    make_skill(root, "plain/inner", name="inner")  # "plain" is a category holding a skill
    bucket = do_install(hscope, base, f"{base}/skills/plain/SKILL.md")
    assert bucket.status == "failed" and "category" in bucket.message
    make_skill(root, "host")
    nested = do_install(hscope, base, f"{base}/skills/plain/SKILL.md", category="host")
    assert nested.status == "failed" and "is a skill, not a category" in nested.message


def test_name_taken_by_a_local_skill_is_refused(hscope: Scope, served: Any) -> None:
    base, _ = served
    make_skill(hscope.home / "skills", "notes/plain", name="plain")
    result = do_install(hscope, base, f"{base}/skills/plain/SKILL.md")
    assert result.status == "failed" and "already exists" in result.message


def test_reinstall_needs_force(hscope: Scope, served: Any) -> None:
    base, _ = served
    url = f"{base}/skills/plain/SKILL.md"
    assert do_install(hscope, base, url).ok
    again = do_install(hscope, base, url)
    assert again.status == "exists"
    assert do_install(hscope, base, url, force=True).ok


def test_name_override_rewrites_frontmatter(hscope: Scope, served: Any) -> None:
    base, _ = served
    result = do_install(hscope, base, f"{base}/skills/plain/SKILL.md", name="my-plain")
    assert result.ok
    text = (hscope.home / "skills" / "my-plain" / "SKILL.md").read_text()
    assert "name: my-plain" in text
    assert load_catalog(hscope).resolve("my-plain").name == "my-plain"


def test_unnamed_url_without_a_terminal_points_at_name(hscope: Scope, served: Any) -> None:
    base, table = served
    table["/SKILL.md"] = b"---\ndescription: d\n---\n\nbody\n"
    result = do_install(hscope, base, f"{base}/SKILL.md")
    assert result.status == "failed" and "--name" in result.message
    asked = do_install(hscope, base, f"{base}/SKILL.md", ask_name=lambda _where: "from-prompt")
    assert asked.ok and (hscope.home / "skills" / "from-prompt").is_dir()


def test_declined_confirmation_leaves_nothing(hscope: Scope, served: Any) -> None:
    base, _ = served
    result = do_install(hscope, base, f"{base}/skills/plain/SKILL.md", confirm=lambda _p: False)
    assert result.status == "declined"
    assert not (hscope.home / "skills" / "plain").exists()
    assert not any((hscope.home / "skills" / ".hub" / "quarantine").iterdir())


def test_inspect_installs_nothing(hscope: Scope, served: Any) -> None:
    base, _ = served
    preview = with_sources(
        hscope, base, lambda s: hub.inspect(hscope, s, f"{base}/skills/risky/SKILL.md")
    )
    assert preview.report.verdict == "caution" and not preview.allowed
    assert "chmod" in preview.render()
    assert not (hscope.home / "skills" / "risky").exists()
    assert HubLock(hscope.home / "skills").get("risky") is None


# -- lifecycle ----------------------------------------------------------------------------------


def test_check_update_and_local_edits(hscope: Scope, served: Any) -> None:
    base, table = served
    url_a, url_b = f"{base}/skills/plain/SKILL.md", f"{base}/skills/other/SKILL.md"
    table["/skills/other/SKILL.md"] = skill_text("other")
    assert do_install(hscope, base, url_a).ok and do_install(hscope, base, url_b).ok
    rows = with_sources(hscope, base, lambda s: hub.check_updates(hscope, s))
    assert {r.name: r.status for r in rows} == {"plain": "up_to_date", "other": "up_to_date"}

    table["/skills/plain/SKILL.md"] = skill_text("plain", "Version two. Read references/more.md.\n")
    table["/skills/other/SKILL.md"] = skill_text("other", "Version two.\n")
    edited = hscope.home / "skills" / "other" / "SKILL.md"
    edited.write_text(edited.read_text() + "\nMy own note.\n")

    rows = with_sources(hscope, base, lambda s: hub.check_updates(hscope, s))
    assert {r.name: r.status for r in rows} == {
        "plain": "update_available",
        "other": "update_available",
    }
    report = with_sources(hscope, base, lambda s: hub.update(hscope, s))
    assert report.updated == ["plain"] and report.kept_local == ["other"]
    assert "Version two" in (hscope.home / "skills" / "plain" / "SKILL.md").read_text()
    assert "My own note." in edited.read_text()

    forced = with_sources(hscope, base, lambda s: hub.update(hscope, s, "other", force=True))
    assert forced.updated == ["other"] and "My own note." not in edited.read_text()


def test_check_reports_orphaned_and_unsafe_without_network(hscope: Scope, served: Any) -> None:
    base, _ = served
    assert do_install(hscope, base, f"{base}/skills/plain/SKILL.md").ok
    root = hscope.home / "skills"
    shutil.rmtree(root / "plain")
    lock = HubLock(root)
    data = json.loads(lock.path.read_text())
    data["installed"]["bad"] = {"name": "bad", "install_path": "../../etc", "source": "url"}
    lock.path.write_text(json.dumps(data))
    rows = asyncio.run(_check_offline(hscope))
    assert {r.name: r.status for r in rows} == {"bad": "unsafe", "plain": "orphaned"}
    assert lock.get("plain") is not None and lock.get("bad") is not None  # never auto-removed


async def _check_offline(scope: Scope) -> list[hub.UpdateRow]:
    sources = build_sources(scope, official=Path("/nonexistent"))
    try:
        return await hub.check_updates(scope, sources)
    finally:
        await sources.aclose()


def test_audit_rescans(hscope: Scope, served: Any) -> None:
    base, _ = served
    assert do_install(hscope, base, f"{base}/skills/plain/SKILL.md").ok
    skill = hscope.home / "skills" / "plain" / "SKILL.md"
    skill.write_text(skill.read_text() + f"\n{DANGEROUS_LINE}\n")
    rows = hub.audit(hscope)
    assert [(r.name, r.report.verdict if r.report else None) for r in rows] == [
        ("plain", "dangerous")
    ]


def test_uninstall_removes_dir_and_entry(hscope: Scope, served: Any) -> None:
    base, _ = served
    assert do_install(hscope, base, f"{base}/skills/plain/SKILL.md").ok
    message = hub.uninstall(hscope, "plain")
    assert message == "removed skills/plain"
    assert not (hscope.home / "skills" / "plain").exists()
    assert HubLock(hscope.home / "skills").get("plain") is None
    assert " UNINSTALL plain url:community " in audit_lines(hscope)[-1]
    with pytest.raises(hub.HubError):
        hub.uninstall(hscope, "plain")


def test_uninstall_refuses_an_entry_pointing_outside(hscope: Scope, tmp_path: Path) -> None:
    root = hscope.home / "skills"
    victim = tmp_path / "victim"
    victim.mkdir()
    root.mkdir(parents=True)
    (root / "evil").symlink_to(victim, target_is_directory=True)
    lock = HubLock(root)
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text(json.dumps({"installed": {"evil": {"install_path": "evil"}}}))
    with pytest.raises(hub.HubError, match="refusing"):
        hub.uninstall(hscope, "evil")
    assert victim.is_dir()


def test_new_skill_reaches_an_open_session_as_a_delta_note(
    hscope: Scope, served: Any, tmp_path: Path
) -> None:
    from indus.core.prompt.builder import PromptBuilder
    from indus.core.prompt.lifecycle import PromptLifecycle
    from indus.learning.skills.prompt_index import (
        IndexDeltaTracker,
        SkillsIndexBuilder,
        catalog_index_source,
        stage_index_delta,
    )
    from tests.unit.learning.skills.test_prompt_index import (
        _ctx,  # pyright: ignore[reportPrivateUsage]
    )

    base, _ = served
    make_skill(hscope.home / "skills", "first", description="Already here.")
    builder = SkillsIndexBuilder(PromptBuilder(install=None), catalog_index_source(hscope))
    lifecycle = PromptLifecycle(builder)
    ctx = _ctx(hscope, tmp_path)
    tracker = IndexDeltaTracker()
    prompt = asyncio.run(lifecycle.restore_or_build(ctx))
    assert stage_index_delta(lifecycle.notes, tracker, prompt, builder.current_index(ctx)) is None

    assert do_install(hscope, base, f"{base}/skills/plain/SKILL.md").ok
    clear_scan_cache()
    note = stage_index_delta(lifecycle.notes, tracker, prompt, builder.current_index(ctx))
    assert note is not None and "- plain: The plain skill." in note
    assert asyncio.run(lifecycle.restore_or_build(ctx)) == prompt  # the prompt stays frozen
