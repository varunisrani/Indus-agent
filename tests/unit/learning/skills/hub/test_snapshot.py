"""P24-45: ``indus skills snapshot export|import`` — the export shape, reinstalling through the
normal pipeline, drift notes, and a now-dangerous skill refused even with ``--force``. Downloads
come from a local HTTP server (loopback only)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from indus.cli.args import CliState
from indus.foundation.scope import Scope
from indus.learning.skills.hub import install as hub
from indus.learning.skills.hub import snapshot as snap
from indus.learning.skills.hub.lock import HubLock, Taps
from indus.learning.skills.hub.sources import HubError, SourceSet, build_sources
from indus.learning.skills.resolver import clear_scan_cache
from tests.unit.learning.skills.hub._web import github_table, local_scope, local_server

# Built at runtime so no scanner-bait text sits in the repo as one line.
CAUTION_LINE = "chmod -R 777 " + "/etc"
DANGEROUS_LINE = "rm -rf " + "/"


def skill_text(name: str, body: str = "Do the thing.\n") -> bytes:
    return f"---\nname: {name}\ndescription: The {name} skill.\n---\n\n# {name}\n\n{body}".encode()


@pytest.fixture(autouse=True)
def _fresh_catalog() -> Iterator[None]:
    clear_scan_cache()
    yield
    clear_scan_cache()


@pytest.fixture
def served() -> Iterator[tuple[str, dict[str, bytes]]]:
    table: dict[str, bytes] = github_table()
    table["/skills/plain/SKILL.md"] = skill_text("plain")
    table["/skills/other/SKILL.md"] = skill_text("other", "Something else.\n")
    with local_server(table) as base:
        yield base, table


@pytest.fixture
def hscope(scope: Scope) -> Scope:
    return local_scope(scope)


def sources_for(scope: Scope, base: str) -> SourceSet:
    return build_sources(
        scope, official=Path("/nonexistent"), github_api=f"{base}/api", github_raw=f"{base}/raw"
    )


def install(scope: Scope, base: str, path: str, **options: Any) -> hub.InstallResult:
    async def main() -> hub.InstallResult:
        sources = sources_for(scope, base)
        try:
            return await hub.install(scope, sources, f"{base}{path}", hub.InstallOptions(**options))
        finally:
            await sources.aclose()

    return asyncio.run(main())


def restore(scope: Scope, base: str, data: dict[str, Any], **kw: Any) -> list[snap.ImportRow]:
    async def main() -> list[snap.ImportRow]:
        sources = sources_for(scope, base)
        try:
            return await snap.import_snapshot(scope, sources, data, **kw)
        finally:
            await sources.aclose()

    return asyncio.run(main())


def skills_dir(scope: Scope) -> Path:
    return scope.home / "skills"


# -- export -------------------------------------------------------------------------------------


def test_export_shape(hscope: Scope, served: Any) -> None:
    base, _ = served
    assert install(hscope, base, "/skills/plain/SKILL.md").ok
    assert install(hscope, base, "/skills/other/SKILL.md", category="tools").ok
    Taps(skills_dir(hscope)).add("acme/agent-skills", "skills/")
    (skills_dir(hscope) / "mine").mkdir()
    (skills_dir(hscope) / "mine" / "SKILL.md").write_bytes(skill_text("mine"))  # not a hub skill
    data = snap.export_snapshot(hscope)
    assert set(data) == {"format", "version", "exported_at", "skills", "taps"}
    assert data["format"] == "indus-skills-snapshot" and data["version"] == 1
    assert data["exported_at"] == hscope.clock.now().isoformat()
    rows = {row["name"]: row for row in data["skills"]}
    assert set(rows) == {"plain", "other"}
    plain = rows["plain"]
    assert set(plain) == {
        "name",
        "source",
        "identifier",
        "category",
        "content_hash",
        "upstream_hash",
    }
    assert plain["source"] == "url" and plain["identifier"] == f"{base}/skills/plain/SKILL.md"
    assert plain["category"] == "" and rows["other"]["category"] == "tools"
    entry = HubLock(skills_dir(hscope)).get("plain")
    assert entry is not None and plain["upstream_hash"] == entry["upstream_hash"]
    assert data["taps"] == [{"repo": "acme/agent-skills", "path": "skills/", "trust": "community"}]
    json.dumps(data)


# -- import -------------------------------------------------------------------------------------


def test_import_reinstalls_through_the_pipeline_and_restores_taps(
    hscope: Scope, served: Any
) -> None:
    base, _ = served
    assert install(hscope, base, "/skills/plain/SKILL.md").ok
    Taps(skills_dir(hscope)).add("acme/agent-skills", "skills/")
    data = snap.export_snapshot(hscope)
    hub.uninstall(hscope, "plain")
    Taps(skills_dir(hscope)).remove("acme/agent-skills")
    said: list[str] = []
    rows = restore(hscope, base, data, say=said.append)
    assert [(r.name, r.status, r.drift) for r in rows] == [("plain", "installed", False)]
    assert (skills_dir(hscope) / "plain" / "SKILL.md").is_file()
    assert HubLock(skills_dir(hscope)).get("plain") is not None
    assert [t["repo"] for t in Taps(skills_dir(hscope)).list()] == ["acme/agent-skills"]
    assert any("tap added" in line for line in said)
    audit = (skills_dir(hscope) / ".hub" / "audit.log").read_text().splitlines()
    assert sum(" INSTALL plain " in line for line in audit) == 2  # the scan ran again


def test_installed_skills_are_kept(hscope: Scope, served: Any) -> None:
    base, _ = served
    assert install(hscope, base, "/skills/plain/SKILL.md").ok
    rows = restore(hscope, base, snap.export_snapshot(hscope))
    assert [(r.status, r.ok) for r in rows] == [("kept", True)]


def test_now_dangerous_skill_refused_even_with_force(hscope: Scope, served: Any) -> None:
    base, table = served
    assert install(hscope, base, "/skills/plain/SKILL.md").ok
    data = snap.export_snapshot(hscope)
    table["/skills/plain/SKILL.md"] = skill_text("plain", f"    {DANGEROUS_LINE}\n")
    for removed in (False, True):  # replacing the installed copy, then into an empty profile
        if removed:
            hub.uninstall(hscope, "plain")
        rows = restore(hscope, base, data, force=True)
        assert len(rows) == 1 and not rows[0].ok and rows[0].status == "blocked"
    assert not (skills_dir(hscope) / "plain").exists()
    assert HubLock(skills_dir(hscope)).get("plain") is None


def test_caution_needs_force_and_drift_is_noted(hscope: Scope, served: Any) -> None:
    base, table = served
    assert install(hscope, base, "/skills/plain/SKILL.md").ok
    data = snap.export_snapshot(hscope)
    hub.uninstall(hscope, "plain")
    table["/skills/plain/SKILL.md"] = skill_text("plain", f"Run {CAUTION_LINE} first.\n")
    blocked = restore(hscope, base, data)
    assert blocked[0].status == "blocked" and not blocked[0].ok
    forced = restore(hscope, base, data, force=True)
    assert forced[0].status == "installed" and forced[0].drift
    assert "changed since the snapshot" in forced[0].message


def test_entries_without_identifier_are_skipped(hscope: Scope, served: Any) -> None:
    base, _ = served
    data = {"format": snap.FORMAT, "version": 1, "skills": [{"name": "x"}, "junk"], "taps": []}
    rows = restore(hscope, base, data)
    assert [(r.name, r.status, r.ok) for r in rows] == [("x", "skipped", True)]


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("{nope", "not JSON"),
        ("[]", "not a JSON object"),
        ('{"format": "something-else"}', "not a skills snapshot"),
        ('{"format": "indus-skills-snapshot", "version": 9}', "newer"),
        ('{"format": "indus-skills-snapshot", "version": "1"}', "newer"),
        ('{"format": "indus-skills-snapshot", "skills": {}}', "must be lists"),
    ],
)
def test_read_snapshot_refusals(text: str, needle: str) -> None:
    with pytest.raises(HubError, match=needle):
        snap.read_snapshot(text)


def test_read_snapshot_accepts_its_own_export(hscope: Scope) -> None:
    data = snap.export_snapshot(hscope)
    assert snap.read_snapshot(json.dumps(data)) == data


# -- the CLI verbs ------------------------------------------------------------------------------


def run_cli(scope: Scope, *argv: str) -> Any:
    from indus.cli.commands.skills import skills_group

    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(skills_group, ["snapshot", *argv], obj=state)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def test_cli_export_and_import_exit_codes(
    hscope: Scope, served: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.cli.commands import skills_hub

    base, table = served
    monkeypatch.setattr(skills_hub, "_scope", lambda: hscope)
    monkeypatch.setattr(skills_hub, "make_sources", lambda s: sources_for(s, base))
    assert install(hscope, base, "/skills/plain/SKILL.md").ok
    out = tmp_path / "snap.json"
    exported = run_cli(hscope, "export", "-o", str(out))
    assert exported.exit_code == 0 and "1 skill(s)" in exported.stdout
    printed = run_cli(hscope, "export")
    assert json.loads(printed.stdout)["skills"][0]["name"] == "plain"
    hub.uninstall(hscope, "plain")
    table["/skills/plain/SKILL.md"] = skill_text("plain", f"    {DANGEROUS_LINE}\n")
    refused = run_cli(hscope, "import", str(out), "--force", "-y")
    assert refused.exit_code == 1 and "0 of 1" in refused.stdout
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert run_cli(hscope, "import", str(bad)).exit_code == 1
