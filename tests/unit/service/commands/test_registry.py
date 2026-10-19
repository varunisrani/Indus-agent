"""The shared slash-command registry (P06-08)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from indus.service.commands.registry import (
    REGISTRY,
    CommandDef,
    CommandRegistry,
    command_word,
    looks_like_command,
)

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "src" / "indus" / "service" / "commands"
PHASE = re.compile(r"^P\d{2}$")


def test_no_name_or_alias_is_used_twice() -> None:
    keys = REGISTRY.keys()
    assert len(keys) == len(set(keys))
    with pytest.raises(ValueError, match="used twice"):
        CommandRegistry(
            [CommandDef("a", "one", "info"), CommandDef("b", "two", "info", aliases=("a",))]
        )


def test_bad_entries_are_refused() -> None:
    with pytest.raises(ValueError):
        CommandRegistry([CommandDef("Upper", "x", "info")])
    with pytest.raises(ValueError):
        CommandRegistry([CommandDef("a", "x", "nope")])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CommandRegistry([CommandDef("a", "x", "info", busy_policy="later")])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CommandRegistry([CommandDef("a", "x", "info", surfaces=frozenset())])
    with pytest.raises(ValueError):
        CommandRegistry([CommandDef("a", " ", "info")])


def test_every_entry_has_a_cli_handler_or_a_later_owner() -> None:
    from indus.cli.slash.dispatch import HANDLERS

    landed = {"P06", "P08", "P09", "P11"}  # phases whose commands have classic CLI handlers
    # commands of a phase landing in slices (P10: /browser; goals, loops, heartbeats, /btw, /moa,
    # /voice; P13: /mcp, /reload-mcp, /plugins; P14: /cron)
    landed_commands = {
        "browser", "btw", "goal", "subgoal", "loop", "heartbeat", "moa", "voice", "mcp",
        "reload-mcp", "plugins", "cron",
    }  # fmt: skip
    for cmd in REGISTRY:
        assert PHASE.match(cmd.owner), cmd.name
        if cmd.owner in landed or cmd.name in landed_commands:
            assert cmd.name in HANDLERS, f"/{cmd.name} is owned by {cmd.owner} but has no handler"
        else:
            assert cmd.owner > "P06", f"/{cmd.name} owner {cmd.owner}"
            assert cmd.name not in HANDLERS
    assert set(HANDLERS) <= {cmd.name for cmd in REGISTRY}


def test_busy_policies_are_known() -> None:
    policies = {cmd.name: cmd.busy_policy for cmd in REGISTRY}
    assert set(policies.values()) <= {"dispatch", "reject", "interrupt_then_dispatch"}
    assert policies["new"] == "interrupt_then_dispatch"
    assert policies["stop"] == "interrupt_then_dispatch"
    assert policies["model"] == "reject"
    assert policies["queue"] == "dispatch"
    assert policies["steer"] == "dispatch"


def test_resolve_aliases_case_insensitive() -> None:
    new = REGISTRY.resolve("new")
    assert new is not None
    assert REGISTRY.resolve("/RESET") is new
    assert REGISTRY.resolve("Reset") is new
    assert REGISTRY.resolve("q") is REGISTRY.resolve("queue")
    assert REGISTRY.resolve("exit") is REGISTRY.resolve("quit")
    assert REGISTRY.resolve("nothing-like-this") is None


def test_surfaces_filter_commands() -> None:
    cli = {cmd.name for cmd in REGISTRY.commands("cli")}
    gateway = {cmd.name for cmd in REGISTRY.commands("gateway")}
    assert "quit" in cli and "quit" not in gateway
    assert "approve" in gateway and "approve" not in cli
    assert len(REGISTRY.commands()) == len(REGISTRY)


def test_completion_lists_aliases_once() -> None:
    names = REGISTRY.keys("cli")
    found = [c.text for c in REGISTRY.complete("/", names)]
    assert len(found) == len(set(found))
    assert sorted(found) == found
    assert "/reset" in found and "/new" in found
    assert [c.text for c in REGISTRY.complete("/re", names)] == [
        "/reasoning",
        "/refine",
        "/reload-mcp",
        "/reload-skills",
        "/reload_mcp",
        "/reset",
        "/resume",
        "/retry",
    ]
    reset = next(c for c in REGISTRY.complete("/res", names) if c.text == "/reset")
    assert reset.meta == "same as /new"
    assert REGISTRY.complete("hello", names) == []


def test_completion_takes_extra_names() -> None:
    found = REGISTRY.complete("/g", ["goal"], extra={"gs": "quick command"})
    assert [(c.text, c.meta) for c in found] == [
        ("/goal", REGISTRY.resolve("goal").description),  # type: ignore[union-attr]
        ("/gs", "quick command"),
    ]


def test_complete_subcommands() -> None:
    names = REGISTRY.keys("cli")
    assert [c.text for c in REGISTRY.complete("/approvals ", names)] == ["manual", "smart", "off"]
    assert [c.text for c in REGISTRY.complete("/APPROVALS s", names)] == ["smart"]
    assert [c.text for c in REGISTRY.complete("/reasoning h", names)] == ["high", "hide"]
    assert REGISTRY.complete("/approvals off now", names) == []
    assert REGISTRY.complete("/unknown ", names) == []


def test_argument_mode_inference() -> None:
    assert CommandDef("a", "x", "info", subcommands=("on", "off")).mode == "options"
    assert CommandDef("a", "x", "info", args_hint="[text|clear]", subcommands=("clear",)).mode == (
        "mixed"
    )
    assert CommandDef("a", "x", "info", args_hint="<question>").mode == "text"
    assert CommandDef("a", "x", "info").mode is None
    assert REGISTRY.resolve("queue").mode == "mixed"  # type: ignore[union-attr]


def test_prefix_matches() -> None:
    names = ["quit", "queue", "q", "version", "verbose"]
    assert REGISTRY.prefix_matches("q", names).kind == "exact"
    assert REGISTRY.prefix_matches("qui", names).names == ("quit",)
    assert REGISTRY.prefix_matches("ver", names).kind == "ambiguous"
    assert REGISTRY.prefix_matches("ver", names).names == ("verbose", "version")
    assert REGISTRY.prefix_matches("zzz", names).kind == "none"
    assert REGISTRY.prefix_matches("", names).kind == "none"


def test_command_word_and_shape() -> None:
    assert command_word("/Title My Name ") == ("title", "My Name")
    assert command_word("/new") == ("new", "")
    assert looks_like_command("/help")
    assert looks_like_command("  /model x")
    assert not looks_like_command("/usr/bin/env")
    assert not looks_like_command("/")
    assert not looks_like_command("hello /help")


HELP_GROUPS = {
    "session": ["new", "clear", "history", "title", "resume", "sessions", "retry", "undo",
                "save", "branch", "stop", "steer", "queue", "compress", "status", "context",
                "btw", "goal", "subgoal", "loop", "heartbeat", "moa", "refine", "journey",
                "rollback", "worktree"],
    "config": ["model", "reasoning", "yolo", "approvals", "fast", "config", "voice"],
    "tools": ["tools", "skills", "reload-skills", "bundles", "memory", "learn", "curator",
              "browser", "checkpoints", "mcp", "reload-mcp", "plugins", "cron"],
    "info": ["help", "usage", "whoami", "version"],
    "exit": ["quit"],
}  # fmt: skip


def test_help_grouping_golden() -> None:
    from indus.cli.slash.dispatch import HANDLERS

    runnable = [c for c in REGISTRY.commands("cli") if c.name in HANDLERS]
    grouped = REGISTRY.by_category(runnable)
    assert list(grouped) == ["session", "config", "tools", "info", "exit"]
    assert {k: [c.name for c in v] for k, v in grouped.items()} == HELP_GROUPS


def test_describe_prefers_the_catalogue() -> None:
    cmd = REGISTRY.resolve("help")
    assert cmd is not None
    assert cmd.description_key == "slash.help"
    assert REGISTRY.describe(cmd) == cmd.description
    assert REGISTRY.describe(cmd, {"slash.help": "Hilfe"}) == "Hilfe"
    assert REGISTRY.describe(cmd, {}) == cmd.description


def _catalogue_section(name: str) -> dict[str, str]:
    """Leaf ``key: "value"`` lines of one top-level section of locales/en.yaml."""
    out: dict[str, str] = {}
    inside = False
    for line in (ROOT / "locales" / "en.yaml").read_text(encoding="utf-8").splitlines():
        if not line.startswith(" ") and line.strip() and not line.startswith("#"):
            inside = line.rstrip() == f"{name}:"
            continue
        if inside and line.startswith("  ") and not line.startswith("   "):
            key, _, value = line.strip().partition(": ")
            out[key] = value.strip().strip('"').replace('\\"', '"')
    return out


def test_english_catalogue_matches_the_registry() -> None:
    section = _catalogue_section("slash")
    assert section == {cmd.name: cmd.description for cmd in REGISTRY}


def test_the_registry_imports_nothing_from_the_cli() -> None:
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                assert not name.startswith("indus.cli"), f"{path.name} imports {name}"
                assert not name.startswith(("indus.tui_bridge", "indus.gateway", "indus.acp"))
