"""P04-11 contract: every registered tool is reachable; no send-message tool; webhook floor."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.tools import toolsets
from indus.tools.builtin import BUILTIN_MODULES, load_builtins
from indus.tools.registry import ToolRegistry
from indus.tools.toolsets import TOOLSETS, WEBHOOK_FLOOR, resolve, toolset_names

SEND_TOOL = "send_message"


@pytest.fixture(autouse=True)
def _fresh_memo() -> Iterator[None]:
    toolsets.clear_memo()
    yield
    toolsets.clear_memo()


@pytest.fixture
def builtins() -> ToolRegistry:
    registry = ToolRegistry()
    load_builtins(registry, BUILTIN_MODULES)
    return registry


def unreachable(registry: ToolRegistry) -> list[str]:
    reachable: set[str] = set()
    for name in toolset_names(registry):
        reachable |= set(resolve(name, registry))
    return [name for name in registry.names() if name not in reachable]


def test_every_registered_builtin_is_reachable(builtins: ToolRegistry) -> None:
    assert unreachable(builtins) == []


def test_every_registered_builtin_reachable_from_all(builtins: ToolRegistry) -> None:
    everything = set(resolve("all", builtins))
    # Opt-in sets (the task board, projects; P24-13/14) are reachable only when named.
    opt_in = {n for n, ts in TOOLSETS.items() if ts.opt_in}
    named = set().union(*(set(resolve(n, builtins)) for n in opt_in))
    assert not named & everything
    assert [n for n in builtins.names() if n not in everything | named] == []


def test_reachability_check_catches_an_orphan() -> None:
    # A tool registered into a fixed-floor toolset is merged nowhere, so the check must flag it.
    registry = ToolRegistry()
    registry.register("visible", "web", {"description": "x"}, lambda args: "")
    registry.register("smuggled", "indus-webhook", {"description": "x"}, lambda args: "")
    assert unreachable(registry) == ["smuggled"]


def test_no_toolset_holds_a_send_message_tool(builtins: ToolRegistry) -> None:
    for name in toolset_names(builtins):
        assert SEND_TOOL not in resolve(name, builtins), name
    assert SEND_TOOL not in builtins.names()
    for toolset in TOOLSETS.values():
        assert SEND_TOOL not in toolset.tools


def test_webhook_bundle_is_exactly_its_floor(builtins: ToolRegistry) -> None:
    assert resolve("indus-webhook", builtins) == sorted(WEBHOOK_FLOOR)
    for risky in ("terminal", "process_manage", "write_file", "patch", "read_file"):
        assert risky not in WEBHOOK_FLOOR


def test_every_bundle_starts_from_core(builtins: ToolRegistry) -> None:
    core = set(resolve(toolsets.CORE, builtins))
    for name in ("indus-cli", "indus-gateway"):
        assert core <= set(resolve(name, builtins)), name
    # Scheduled jobs: everything in core but the question back to a person (P14-11) and the
    # scheduling tool (R07.15; the cron runner adds the `cronjob` toolset only on opt-in).
    cron = set(resolve("indus-cron", builtins))
    assert core - {"clarify", "cronjob_manage"} <= cron
    assert not {"clarify", "cronjob_manage"} & cron
