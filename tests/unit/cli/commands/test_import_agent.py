"""P24-18: `indus import-agent` — preview, mappings, no credentials, conflicts and `--sync`."""

from __future__ import annotations

import builtins
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cli.importers import agents as rules
from indus.core.prompt import texts
from indus.foundation import config
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.learning.memory.store import MemoryStore
from tests.unit.cli.commands._helpers_d import Result, run

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "import_agent"
# Built at run time: never commit a credential-shaped literal.
FAKE_TOKEN = "ghp" + "_" + "Zq7" * 12


@pytest.fixture(autouse=True)
def _fresh_config_cache() -> Iterator[None]:
    loader.reset()
    yield
    loader.reset()


def _home(tmp_path: Path, name: str) -> Path:
    target = tmp_path / f"{name}-user"
    shutil.copytree(FIXTURES / name, target)
    return target


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = _home(tmp_path, "claude_code_home")
    path = home / ".claude.json"
    path.write_text(path.read_text().replace("set-at-test-time", FAKE_TOKEN))
    (home / ".claude" / "skills" / "review" / ".env").write_text("NOTE=never copied\n")
    return home


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    return _home(tmp_path, "codex_home")


def _scope(scope: Scope, user_home: Path) -> Scope:
    return scope.with_env(scope.env.with_overlay({"HOME": str(user_home)}))


def _settings(scope: Scope) -> Any:
    loader.reset()
    return config.load(scope)


def _all_text(folder: Path) -> str:
    chunks: list[str] = []
    for path in folder.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(errors="replace"))
    return "\n".join(chunks)


def _import(scope: Scope, *argv: str, tty: bool = False) -> Result:
    return run(scope, "import-agent", *argv, tty=tty)


# -- preview ---------------------------------------------------------------------------------------


def test_the_claude_code_preview_lists_every_mapping(scope: Scope, claude_home: Path) -> None:
    s = _scope(scope, claude_home)
    out = _import(s, "claude-code", "--dry-run")
    assert out.code == 0, out.err
    text = out.out
    assert "Preview (nothing has been changed yet):" in text
    for line in (
        "import    instructions     ~/.claude/CLAUDE.md -> PERSONA.md (persona overlay)",
        "import    allow rules      ~/.claude/settings.json permissions.allow -> "
        "command_allowlist: git status, npm run test, npm run test *",
        "import    deny rules       ~/.claude/settings.json permissions.deny -> "
        "approvals.deny: curl, curl *",
        "import    mcp server       ~/.claude.json (github) -> mcp_servers.github",
        "import    mcp server       ~/.claude.json (docs) -> mcp_servers.docs",
        "import    skill            ~/.claude/skills/review -> skills/claude-code-imports/review",
        "skip      slash commands   ~/.claude/commands -> -",
    ):
        assert line in text, f"{line!r} not in\n{text}"
    assert "not mapped: Read(~/notes/**) (Read rules have no Indus equivalent), Bash" in text
    assert "left behind: .env (a credential file)" in text
    assert "credential headers left out (add them as ${NAME}): Authorization" in text
    assert "Summary: 6 to import, 1 skipped" in text
    assert not (s.home / "config.yaml").exists()
    assert not (s.home / "PERSONA.md").exists()
    assert not (s.home / rules.MANIFEST_FILE).exists()
    assert not (s.home / "skills" / "claude-code-imports").exists()


def test_the_codex_preview_lists_every_mapping(scope: Scope, codex_home: Path) -> None:
    s = _scope(scope, codex_home)
    out = _import(s, "codex", "--dry-run")
    assert out.code == 0, out.err
    for line in (
        "import    instructions     ~/.codex/AGENTS.md -> PERSONA.md (persona overlay)",
        "import    mcp server       ~/.codex/config.toml (files) -> mcp_servers.files",
        "import    mcp server       ~/.codex/config.toml (tracker) -> mcp_servers.tracker",
        "import    memories         ~/.codex/memories -> memories/MEMORY.md (saved notes)",
        "import    skill            ~/.codex/skills/deploy-check -> "
        "skills/codex-imports/deploy-check",
        "skip      custom prompts   ~/.codex/prompts -> -",
    ):
        assert line in out.out, f"{line!r} not in\n{out.out}"


def test_without_a_terminal_the_import_needs_yes(scope: Scope, claude_home: Path) -> None:
    s = _scope(scope, claude_home)
    out = _import(s, "claude-code")
    assert out.code == 1
    assert "Preview (nothing has been changed yet):" in out.out
    assert "--yes" in out.err
    assert not (s.home / "config.yaml").exists()
    assert not (s.home / "PERSONA.md").exists()


def test_a_terminal_answer_of_no_changes_nothing(scope: Scope, claude_home: Path) -> None:
    s = _scope(scope, claude_home)
    out = run(s, "import-agent", "claude-code", tty=True, input="n\n")
    assert out.code == 1
    assert "Nothing was changed." in out.out
    assert not (s.home / "PERSONA.md").exists()
    yes = run(s, "import-agent", "claude-code", tty=True, input="y\n")
    assert yes.code == 0, yes.out + yes.err
    assert (s.home / "PERSONA.md").exists()


def test_detection_and_guidance(scope: Scope, tmp_path: Path, claude_home: Path) -> None:
    empty = tmp_path / "empty-home"
    empty.mkdir()
    none = _import(_scope(scope, empty), "--dry-run")
    assert none.code == 1 and "No Claude Code" in none.err
    (claude_home / ".codex").mkdir()
    both = _import(_scope(scope, claude_home), "--dry-run")
    assert both.code == 1 and "indus import-agent codex" in both.err
    shutil.rmtree(claude_home / ".codex")
    one = _import(_scope(scope, claude_home), "--dry-run")
    assert one.code == 0 and "Import from Claude Code" in one.out
    usage = _import(_scope(scope, claude_home), "--source", str(claude_home / ".claude"))
    assert usage.code == 2


# -- what an import writes -------------------------------------------------------------------------


def test_a_token_in_an_mcp_env_is_imported_by_name_only(scope: Scope, claude_home: Path) -> None:
    s = _scope(scope, claude_home)
    out = _import(s, "claude-code", "--yes")
    assert out.code == 0, out.out + out.err
    servers = _settings(s).get("mcp_servers")
    assert servers["github"]["env"] == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}",
        "GITHUB_HOST": "${GITHUB_HOST}",
    }
    assert servers["docs"]["headers"] == {"X-Team": "blue"}
    assert servers["docs"]["transport"] == "http"
    assert FAKE_TOKEN not in _all_text(s.home)
    assert FAKE_TOKEN not in out.out + out.err
    assert "set-at-test-time" not in _all_text(s.home)


def test_credential_files_are_never_opened(
    scope: Scope, claude_home: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_open = builtins.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(os.fspath(file) if isinstance(file, str | os.PathLike) else str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    real_read_bytes = Path.read_bytes

    def spy_read_bytes(self: Path) -> bytes:
        opened.append(str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
    real_path_open = Path.open

    def spy_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(self))
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_path_open)
    for home, agent in ((claude_home, "claude-code"), (codex_home, "codex")):
        s = _scope(scope, home)
        assert _import(s, agent, "--yes").code == 0
    monkeypatch.undo()
    names = {Path(p).name for p in opened}
    assert {"settings.json", "config.toml", "SKILL.md", "notes.md"} <= names  # the spy saw reads
    assert names & {".credentials.json", "auth.json", ".env"} == set()
    skill = scope.home / "skills" / "claude-code-imports" / "review"
    assert (skill / "SKILL.md").is_file() and (skill / "checklist.md").is_file()
    assert not (skill / ".env").exists()


def test_permission_rules_map_and_the_floors_still_come_first(
    scope: Scope, claude_home: Path
) -> None:
    settings = claude_home / ".claude" / "settings.json"
    settings.write_text(
        '{"permissions": {"allow": ["Bash(git status)", "Bash(rm:*)"], "deny": ["Bash(curl:*)"]}}'
    )
    s = _scope(scope, claude_home)
    assert _import(s, "claude-code", "--yes").code == 0
    view = _settings(s)
    assert view.get("command_allowlist") == ["git status", "rm", "rm *"]
    assert view.get("approvals.deny") == ["curl", "curl *"]
    assert run(s, "approvals", "test", "--", "git", "status").code == 0
    assert run(s, "approvals", "test", "--", "curl", "https://x.example").code == 3
    assert run(s, "approvals", "test", "--", "rm", "-rf", "/").code == 3  # hardline floor


@pytest.mark.parametrize(
    ("rule", "patterns"),
    [
        ("Bash(git status)", ["git status"]),
        ("Bash(npm run test:*)", ["npm run test", "npm run test *"]),
        ("Bash(git  log   *)", ["git log *"]),
        ("Bash", []),
        ("Bash(*)", []),
        ("Bash(:*)", []),
        ("Edit(src/**)", []),
        ("mcp__github__create_issue", []),
    ],
)
def test_map_permission(rule: str, patterns: list[str]) -> None:
    mapped, why = rules.map_permission(rule)
    assert mapped == patterns
    assert bool(why) == (not patterns)


def test_existing_comments_survive_the_settings_write(scope: Scope, codex_home: Path) -> None:
    s = _scope(scope, codex_home)
    (s.home / "config.yaml").write_text("# my settings\nmodel: kept-model  # inline\n")
    assert _import(s, "codex", "--yes").code == 0
    text = (s.home / "config.yaml").read_text()
    assert "# my settings" in text and "# inline" in text
    servers = _settings(s).get("mcp_servers")
    assert servers["files"]["env"] == {"LOG_LEVEL": "${LOG_LEVEL}"}
    assert servers["tracker"]["headers"] == {"Authorization": "Bearer ${TRACKER_TOKEN}"}


def test_the_persona_overlay_keeps_the_identity_and_is_replaced_on_reimport(
    scope: Scope, claude_home: Path
) -> None:
    s = _scope(scope, claude_home)
    assert _import(s, "claude-code", "--yes").code == 0
    persona = (s.home / "PERSONA.md").read_text()
    assert persona.startswith(texts.DEFAULT_IDENTITY.rstrip())
    assert "## Instructions brought over from Claude Code" in persona
    assert "Prefer small, reviewed changes." in persona
    again = _import(s, "claude-code", "--dry-run")
    assert "skip      instructions" in again.out and "already up to date" in again.out
    (claude_home / ".claude" / "CLAUDE.md").write_text("Use tabs.\n")
    assert _import(s, "claude-code", "--yes").code == 0
    persona = (s.home / "PERSONA.md").read_text()
    assert persona.count("(begin) -->") == 1
    assert "Use tabs." in persona and "Prefer small" not in persona


def test_a_users_own_persona_is_kept(scope: Scope, codex_home: Path) -> None:
    s = _scope(scope, codex_home)
    (s.home / "PERSONA.md").write_text("You are Ada, a careful engineer.\n")
    assert _import(s, "codex", "--yes").code == 0
    persona = (s.home / "PERSONA.md").read_text()
    assert persona.startswith("You are Ada, a careful engineer.")
    assert "Keep functions short" in persona


def test_memories_become_entries_and_respect_both_caps(scope: Scope, codex_home: Path) -> None:
    s = _scope(scope, codex_home)
    (s.home / "config.yaml").write_text("memory:\n  notes_char_limit: 30000\n")
    big = "\n".join(f"- fact number {i:05d} " + "x" * 80 for i in range(400))
    (codex_home / ".codex" / "memories" / "zz-big.md").write_text(big)  # read after notes.md
    out = _import(s, "codex", "--yes")
    assert out.code == 0, out.out
    assert "entries past the 20,000-character import cap left out" in out.out
    _settings(s)
    entries = MemoryStore.for_scope(_scope_with(s)).entries("memory")
    assert "Project facts / Build: The build uses make." in entries
    assert "Project facts / Build: The staging server is rebuilt every night." in entries
    assert not any("make release" in e for e in entries)
    assert not any("| key |" in e for e in entries)
    assert sum(len(e) for e in entries) <= rules.CHAR_CAP


def _scope_with(s: Scope) -> Scope:
    return s.with_config(config.load(s))


def test_memories_that_do_not_fit_the_store_are_reported(scope: Scope, codex_home: Path) -> None:
    s = _scope(scope, codex_home)
    big = "\n".join(f"- fact number {i:05d} " + "y" * 80 for i in range(100))
    (codex_home / ".codex" / "memories" / "big.md").write_text(big)
    out = _import(s, "codex", "--yes")
    assert "do not fit memory.notes_char_limit (2,200 chars)" in out.out
    entries = MemoryStore.for_scope(_scope_with(s)).entries("memory")
    assert 0 < sum(len(e) for e in entries) <= 2200


def test_conflicts_and_overwrite(scope: Scope, claude_home: Path) -> None:
    s = _scope(scope, claude_home)
    (s.home / "config.yaml").write_text("mcp_servers:\n  github:\n    command: mine\n")
    out = _import(s, "claude-code", "--yes")
    assert "conflict  mcp server       ~/.claude.json (github)" in out.out
    assert _settings(s).get("mcp_servers")["github"] == {"command": "mine"}
    skill = s.home / "skills" / "claude-code-imports" / "review"
    (skill / "SKILL.md").write_text("edited here\n")
    again = _import(s, "claude-code", "--yes")
    assert "changed here since it was imported" in again.out
    assert (skill / "SKILL.md").read_text() == "edited here\n"
    forced = _import(s, "claude-code", "--yes", "--overwrite")
    assert forced.code == 0
    assert _settings(s).get("mcp_servers")["github"]["command"] == "npx"
    assert "Read the diff" in (skill / "SKILL.md").read_text()


def test_an_unreadable_settings_file_is_left_alone(scope: Scope, claude_home: Path) -> None:
    s = _scope(scope, claude_home)
    broken = "model: [unclosed\n"
    (s.home / "config.yaml").write_text(broken)
    out = _import(s, "claude-code", "--yes")
    assert out.code == 1
    assert "the settings file cannot be read right now" in out.out
    assert (s.home / "config.yaml").read_text() == broken
    assert (s.home / "PERSONA.md").exists()  # file mappings still ran


@pytest.mark.parametrize(
    ("raw", "refusal"),
    [
        ({"command": "srv", "args": ["--token=" + FAKE_TOKEN]}, "argument looks like a credential"),
        ({"url": "https://user:pw@host.example/mcp"}, "URL looks like it carries a credential"),
        ({"url": f"https://host.example/mcp?key={FAKE_TOKEN}"}, "URL looks like"),
        ({"note": "nothing"}, "neither a command nor a url"),
    ],
)
def test_servers_that_would_carry_a_credential_are_refused(
    raw: dict[str, Any], refusal: str
) -> None:
    entry, _notes, why = rules.translate_server("s", raw, project=False)
    assert entry is None and refusal in why


def test_project_files(scope: Scope, claude_home: Path, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".claude").mkdir(parents=True)
    (project / "CLAUDE.md").write_text("Project rules.\n")
    (project / ".claude" / "settings.json").write_text(
        '{"permissions": {"allow": ["Bash(make deploy)"], "deny": ["Bash(git push:*)"]}}'
    )
    (project / ".claude" / "settings.local.json").write_text(
        '{"permissions": {"allow": ["Bash(make test)"]}}'
    )
    (project / ".mcp.json").write_text('{"mcpServers": {"db": {"command": "db-mcp"}}}')
    s = _scope(scope, claude_home)
    out = _import(s, "claude-code", "--project", str(project), "--yes")
    assert out.code == 0, out.out + out.err
    assert f"in place  project context  {project / 'CLAUDE.md'}" in out.out
    assert "allow rules a repository commits are not imported" in out.out
    view = _settings(s)
    assert "make deploy" not in view.get("command_allowlist")
    assert "make test" in view.get("command_allowlist")
    assert {"git push", "git push *"} <= set(view.get("approvals.deny"))
    assert view.get("mcp_servers")["db"] == {"command": "db-mcp", "enabled": False}
    (project / "AGENTS.md").write_text("Agents.\n")
    shadowed = _import(s, "claude-code", "--project", str(project), "--dry-run")
    assert "AGENTS.md in the project is read first" in shadowed.out


# -- sync ------------------------------------------------------------------------------------------


def test_sync_touches_only_changed_sources(scope: Scope, codex_home: Path) -> None:
    s = _scope(scope, codex_home)
    assert _import(s, "codex", "--yes").code == 0
    manifest = rules.Manifest.read(s.home)
    assert manifest.agents["codex"]["source"] == str((codex_home / ".codex").resolve())
    config_file = s.home / "config.yaml"
    skill = s.home / "skills" / "codex-imports" / "deploy-check" / "SKILL.md"
    before = (config_file.stat().st_mtime_ns, skill.stat().st_mtime_ns)
    unchanged = _import(s, "--sync")
    assert unchanged.code == 0 and "Codex: unchanged since the last import." in unchanged.out
    (codex_home / ".codex" / "AGENTS.md").write_text("Name things plainly. Always.\n")
    synced = _import(s, "--sync")
    assert synced.code == 0, synced.out + synced.err
    assert "Codex: 1 changed source(s)" in synced.out
    assert "imported  instructions" in synced.out
    assert "mcp server" not in synced.out and "skill" not in synced.out
    assert (config_file.stat().st_mtime_ns, skill.stat().st_mtime_ns) == before
    assert "Always." in (s.home / "PERSONA.md").read_text()
    assert "unchanged since the last import" in _import(s, "--sync").out


def test_sync_refreshes_an_untouched_skill_copy_and_spares_an_edited_one(
    scope: Scope, codex_home: Path
) -> None:
    s = _scope(scope, codex_home)
    assert _import(s, "codex", "--yes").code == 0
    source = codex_home / ".codex" / "skills" / "deploy-check" / "SKILL.md"
    copy = s.home / "skills" / "codex-imports" / "deploy-check" / "SKILL.md"
    source.write_text(source.read_text() + "\nAlso check the dashboards.\n")
    assert _import(s, "--sync").code == 0
    assert "dashboards" in copy.read_text()
    copy.write_text("my own version\n")
    source.write_text(source.read_text() + "\nAnd the alerts.\n")
    out = _import(s, "--sync")
    assert "changed here since it was imported" in out.out
    assert copy.read_text() == "my own version\n"


def test_sync_without_records_and_dry_run(scope: Scope, codex_home: Path) -> None:
    s = _scope(scope, codex_home)
    nothing = _import(s, "--sync")
    assert nothing.code == 0 and "No imports are recorded yet" in nothing.out
    assert _import(s, "codex", "--yes").code == 0
    (codex_home / ".codex" / "AGENTS.md").write_text("Changed.\n")
    dry = _import(s, "--sync", "--dry-run")
    assert "(dry run)" in dry.out and "import    instructions" in dry.out
    assert "Changed." not in (s.home / "PERSONA.md").read_text()


def test_a_server_in_both_claude_files_comes_from_claude_json(
    scope: Scope, claude_home: Path
) -> None:
    (claude_home / ".claude" / "settings.json").write_text(
        '{"mcpServers": {"github": {"command": "other"}}}'
    )
    s = _scope(scope, claude_home)
    out = _import(s, "claude-code", "--yes")
    assert "also defined in ~/.claude.json, which is used" in out.out
    assert _settings(s).get("mcp_servers")["github"]["command"] == "npx"


def test_sync_sees_a_source_file_that_appeared_after_the_import(
    scope: Scope, codex_home: Path
) -> None:
    (codex_home / ".codex" / "config.toml").unlink()
    s = _scope(scope, codex_home)
    assert _import(s, "codex", "--yes").code == 0
    assert _settings(s).get("mcp_servers") == {}
    (codex_home / ".codex" / "config.toml").write_text('[mcp_servers.late]\ncommand = "late-mcp"\n')
    out = _import(s, "--sync")
    assert out.code == 0 and "imported  mcp server" in out.out
    assert _settings(s).get("mcp_servers")["late"] == {"command": "late-mcp"}
