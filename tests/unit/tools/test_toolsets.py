"""P04-11: toolset table and resolver."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.tools import toolsets
from indus.tools.registry import ToolRegistry, scope_key
from indus.tools.toolsets import (
    BROWSER_TOOLS,
    CORE,
    CORE_TOOLS,
    TOOLSETS,
    WEBHOOK_FLOOR,
    Toolset,
    get_toolset,
    is_known,
    persistable,
    resolve,
    select,
    toolset_names,
)
from tests.fakes.clock import FakeClock
from tests.unit.tools._helpers import echo, other_scope, schema


@pytest.fixture(autouse=True)
def _fresh_memo() -> Iterator[None]:
    toolsets.clear_memo()
    yield
    toolsets.clear_memo()


@pytest.fixture
def reg(fake_clock: FakeClock) -> ToolRegistry:
    return ToolRegistry(clock=fake_clock)


@pytest.fixture
def extra_sets(monkeypatch: pytest.MonkeyPatch) -> dict[str, Toolset]:
    table = dict(TOOLSETS)
    table["loop_a"] = Toolset("a", ("tool_a",), ("loop_b",))
    table["loop_b"] = Toolset("b", ("tool_b",), ("loop_a",))
    table["top"] = Toolset("diamond top", (), ("left", "right"))
    table["left"] = Toolset("left", ("l",), ("bottom",))
    table["right"] = Toolset("right", ("r",), ("bottom",))
    table["bottom"] = Toolset("bottom", ("b1", "b2"))
    table["desk"] = Toolset("desktop only", ("pane_tool",), platforms=frozenset({"desktop"}))
    monkeypatch.setattr(toolsets, "TOOLSETS", table)
    return table


def test_core_list_is_the_p05_tool_set_plus_delegation() -> None:
    assert set(CORE_TOOLS) == {
        "terminal",
        "process_manage",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "web_search",
        "web_extract",
        "todo_list",
        "clarify",
        "memory",  # P09-02
        "session_search",  # P09-25
        "skills_list",  # P09-08
        "skill_view",  # P09-08
        "skill_manage",  # P09-11
        "execute_code",  # P10-08
        "delegate_task",  # P10-01
        *BROWSER_TOOLS,  # P10-12..14
        "vision_analyze",  # P10-15
        "image_generate",  # P10-16
        "text_to_speech",  # P10-17
        "computer_use",  # P10-20
    }
    assert set(resolve(CORE)) == set(CORE_TOOLS)


def test_named_sets() -> None:
    assert resolve("terminal") == ["process_manage", "terminal"]
    assert resolve("file") == ["patch", "read_file", "search_files", "write_file"]
    assert resolve("web") == ["web_extract", "web_search"]
    assert resolve("search") == ["web_search"]
    assert resolve("browser") == sorted(BROWSER_TOOLS)
    assert resolve("memory") == ["memory"]
    assert resolve("session_search") == ["session_search"]
    assert resolve("skills") == ["skill_manage", "skill_view", "skills_list"]
    assert resolve("code_execution") == ["execute_code"]
    assert resolve("vision") == ["vision_analyze"]
    assert resolve("image_gen") == ["image_generate"]
    assert resolve("video_gen") == ["video_edit", "video_extend", "video_generate"]
    assert resolve("tts") == ["text_to_speech"]
    assert resolve("computer_use") == ["computer_use"]
    assert set(resolve("debugging")) == {
        "terminal",
        "process_manage",
        "web_search",
        "web_extract",
        "read_file",
        "write_file",
        "patch",
        "search_files",
    }
    assert "terminal" not in resolve("safe") and "write_file" not in resolve("safe")


def test_platform_bundles() -> None:
    core = set(CORE_TOOLS)
    assert set(resolve("indus-cli")) == core
    assert set(resolve("indus-cron")) == core
    assert set(resolve("indus-acp")) == core - {
        "clarify",
        "image_generate",
        "text_to_speech",
        "computer_use",
    }
    assert set(resolve("indus-api-server")) == core - {"clarify", "text_to_speech", "computer_use"}
    assert resolve("indus-webhook") == sorted(WEBHOOK_FLOOR)
    assert set(resolve("indus-gateway")) == core | set(WEBHOOK_FLOOR)
    assert set(resolve("coding")) == core - {"image_generate", "text_to_speech", "computer_use"}
    assert TOOLSETS["coding"].posture


def test_cycle_resolves(extra_sets: dict[str, Toolset]) -> None:
    assert resolve("loop_a") == ["tool_a", "tool_b"]
    assert resolve("loop_b") == ["tool_a", "tool_b"]


def test_diamond_resolves(extra_sets: dict[str, Toolset]) -> None:
    assert resolve("top") == ["b1", "b2", "l", "r"]


def test_unknown_is_empty() -> None:
    assert resolve("no_such_set") == []
    assert not is_known("no_such_set")
    assert is_known("all") and is_known("*") and is_known("web")


def test_all_skips_platform_gated(extra_sets: dict[str, Toolset]) -> None:
    everything = resolve("all")
    assert "pane_tool" not in everything
    assert set(CORE_TOOLS) <= set(everything)
    assert resolve("*") == everything
    assert "pane_tool" in resolve("desk")


def test_platform_gating_in_select(extra_sets: dict[str, Toolset]) -> None:
    assert "pane_tool" not in select(["desk"]).tools
    assert "pane_tool" in select(["desk"], platform="desktop").tools


def test_disabled_subtracted_last() -> None:
    picked = select(["indus-cli", "debugging"], ["terminal"])
    assert "terminal" not in picked.tools and "process_manage" not in picked.tools
    assert "read_file" in picked.tools


def test_disabling_a_bundle_keeps_core(extra_sets: dict[str, Toolset]) -> None:
    extra_sets["indus-chat"] = Toolset("chat", ("chat_extra",), (CORE,))
    picked = select(["indus-chat"], ["indus-chat"])
    assert picked.tools == frozenset(CORE_TOOLS)


def test_disabling_the_posture_keeps_core() -> None:
    assert select(["indus-cli"], ["coding"]).tools == frozenset(CORE_TOOLS)


def test_unknown_names_reported() -> None:
    picked = select(["web", "nonsense"], ["also_nonsense"])
    assert picked.unknown == ("nonsense", "also_nonsense")
    assert picked.tools == {"web_search", "web_extract"}


def test_none_enabled_means_all() -> None:
    assert select(None).tools == frozenset(resolve("all"))


def test_registry_members_merged_into_static_set(reg: ToolRegistry) -> None:
    reg.register("fetch_pdf", "web", schema(), echo)
    assert "fetch_pdf" in resolve("web", reg)
    assert "fetch_pdf" in resolve("indus-cli", reg)  # bundles carry registered members (D-16.74)
    assert "fetch_pdf" in resolve("indus-api-server", reg)
    assert "fetch_pdf" not in resolve("indus-webhook", reg)  # fixed floor (R02.27)
    toolset = get_toolset("web", reg)
    assert toolset is not None and "fetch_pdf" in toolset.tools


def test_registry_only_toolset(reg: ToolRegistry) -> None:
    reg.register("weather", "plugin_weather", schema(), echo)
    assert resolve("plugin_weather", reg) == ["weather"]
    assert "plugin_weather" in toolset_names(reg)
    assert is_known("plugin_weather", reg)
    assert "weather" in resolve("all", reg)


def test_generation_bump_invalidates_memo(reg: ToolRegistry) -> None:
    assert resolve("web", reg) == ["web_extract", "web_search"]
    reg.register("fetch_pdf", "web", schema(), echo)
    assert "fetch_pdf" in resolve("web", reg)
    reg.deregister("fetch_pdf")
    assert resolve("web", reg) == ["web_extract", "web_search"]


def test_profiles_resolve_their_own_overlays(
    reg: ToolRegistry, scope: Scope, tmp_path: Path, fake_clock: FakeClock
) -> None:
    other = other_scope(tmp_path, "other", fake_clock)
    reg.register("plugged", "web", schema(), echo, profile=scope_key(scope))
    assert "plugged" in resolve("web", reg, scope)
    assert "plugged" not in resolve("web", reg, other)


def test_memo_is_bounded(reg: ToolRegistry) -> None:
    for i in range(toolsets.MEMO_MAX + 10):
        resolve(f"unknown_{i}", reg)
    assert len(toolsets._memo) <= toolsets.MEMO_MAX  # pyright: ignore[reportPrivateUsage]


def test_returned_lists_are_private() -> None:
    first = resolve("web")
    first.append("injected")
    assert resolve("web") == ["web_extract", "web_search"]


def test_posture_never_persists() -> None:
    assert persistable(["indus-cli", "coding", "web"]) == ["indus-cli", "web"]
    assert persistable(["unknown"]) == ["unknown"]


def test_no_send_message_tool_anywhere() -> None:
    for name, toolset in TOOLSETS.items():
        assert "send_message" not in toolset.tools, name
    assert "send_message" not in resolve("all")
