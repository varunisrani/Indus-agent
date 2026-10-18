"""Loop guardrails (P04-18)."""

from __future__ import annotations

import pytest

from indus.core.guardrails import (
    GuardDecision,
    GuardrailConfig,
    ToolGuard,
    blocked_result,
    guidance,
    is_attended,
    signature,
)

HARD = GuardrailConfig(hard_stops=True)
SOFT = GuardrailConfig(hard_stops=False)


def fail(
    guard: ToolGuard, tool: str = "fetch", args: dict[str, object] | None = None
) -> GuardDecision:
    decision, _ = guard.after(
        tool,
        args if args is not None else {"u": 1},
        '{"error": "no"}',
        failed=True,
        read_only=False,
        mutating=False,
    )
    return decision


def test_signature_ignores_key_order() -> None:
    assert signature("t", {"a": 1, "b": 2}) == signature("t", {"b": 2, "a": 1})
    assert signature("t", {"a": 1}) != signature("u", {"a": 1})
    assert signature("t", None) == signature("t", {})


def test_exact_failure_warns_at_two_and_blocks_at_five() -> None:
    guard = ToolGuard(HARD)
    actions = [fail(guard).action for _ in range(5)]
    assert actions[:2] == ["allow", "warn"]
    assert guard.before("fetch", {"u": 1}, read_only=False).action == "block"
    assert guard.halt is not None and guard.halt.code == "exact_failure_block"
    assert guard.before("fetch", {"u": 2}, read_only=False).runs


def test_same_tool_failure_warns_at_three_and_halts_at_eight() -> None:
    guard = ToolGuard(HARD)
    actions = [fail(guard, args={"u": n}).action for n in range(8)]
    assert actions[2] == "warn" and actions[1] == "allow"
    assert actions[7] == "halt"
    assert guard.halt is not None and guard.halt.count == 8


def test_noisy_tools_never_halt_on_different_arguments() -> None:
    guard = ToolGuard(HARD)
    actions = [fail(guard, "terminal", {"cmd": n}).action for n in range(12)]
    assert "halt" not in actions and actions[-1] == "warn"


def test_attended_surfaces_only_warn() -> None:
    guard = ToolGuard(SOFT)
    for n in range(12):
        fail(guard, args={"u": n % 2})
    assert guard.halt is None
    assert guard.before("fetch", {"u": 1}, read_only=False).runs
    assert GuardrailConfig.for_platform("cli").hard_stops is False
    assert GuardrailConfig.for_platform("cron").hard_stops is True
    assert GuardrailConfig.for_platform("gateway").hard_stops is True
    assert GuardrailConfig.for_platform("webhook").hard_stops is True
    assert is_attended("") and is_attended("desktop") and not is_attended("telegram")


def test_idle_no_progress_for_read_only_tools() -> None:
    guard = ToolGuard(HARD)
    results = []
    for _ in range(5):
        decision, _ = guard.after(
            "look", {"q": 1}, "same", failed=False, read_only=True, mutating=False
        )
        results.append(decision.action)
    assert results[:2] == ["allow", "warn"]
    assert guard.before("look", {"q": 1}, read_only=True).action == "block"
    # a different answer restarts the count
    other = ToolGuard(HARD)
    other.after("look", {"q": 1}, "a", failed=False, read_only=True, mutating=False)
    decision, _ = other.after("look", {"q": 1}, "b", failed=False, read_only=True, mutating=False)
    assert decision.action == "allow"


def test_mutating_success_resets_failure_streaks() -> None:
    guard = ToolGuard(HARD)
    for _ in range(4):
        fail(guard)
    guard.after("write", {"p": 1}, "ok", failed=False, read_only=False, mutating=True)
    assert guard.before("fetch", {"u": 1}, read_only=False).runs
    assert fail(guard).action == "allow"  # the exact streak started again


@pytest.mark.parametrize(("cap", "allowed"), [(3, 3), (0, 10)])
def test_web_search_cap(cap: int, allowed: int) -> None:
    guard = ToolGuard(GuardrailConfig(max_web_searches=cap))
    verdicts = [guard.before("web_search", {"q": n}, read_only=True).runs for n in range(10)]
    assert verdicts.count(True) == allowed
    if cap:
        assert guard.halt is not None and guard.halt.code == "web_search_cap"


def test_subagent_cap_counts_tasks_in_a_batch_call() -> None:
    guard = ToolGuard(GuardrailConfig(max_subagents=4))
    assert guard.before("delegate_task", {"tasks": [1, 2, 3]}, read_only=False).runs
    assert guard.before("delegate_task", {"goal": "x"}, read_only=False).runs
    assert guard.before("delegate_task", {"action": "list"}, read_only=False).runs
    assert not guard.before("delegate_task", {"goal": "y"}, read_only=False).runs


def test_identical_bytes_become_a_pointer_stub() -> None:
    guard = ToolGuard(SOFT)
    text = "row\n" * 200
    _, first = guard.after(
        "look", {"q": 1}, text, failed=False, read_only=True, mutating=False, call_id="c1"
    )
    _, second = guard.after(
        "look", {"q": 1}, text, failed=False, read_only=True, mutating=False, call_id="c2"
    )
    assert first is None
    assert second is not None and "c1" in second and len(second) < 300
    _, short = ToolGuard(SOFT).after(
        "look", {}, "tiny", failed=False, read_only=True, mutating=False
    )
    assert short is None
    g = ToolGuard(SOFT)
    g.after("look", {}, text, failed=True, read_only=True, mutating=False)
    _, failed_repeat = g.after("look", {}, text, failed=True, read_only=True, mutating=False)
    assert failed_repeat is None


def test_guidance_and_block_texts() -> None:
    guard = ToolGuard(HARD)
    fail(guard)
    warn = fail(guard)
    assert warn.action == "warn" and guidance(warn).startswith("\n\n[")
    assert guidance(GuardDecision()) == ""
    for _ in range(3):
        fail(guard)
    block = guard.before("fetch", {"u": 1}, read_only=False)
    assert '"guardrail": "exact_failure_block"' in blocked_result(block)


def test_reset_starts_a_new_turn() -> None:
    guard = ToolGuard(HARD)
    for _ in range(5):
        fail(guard)
    guard.before("fetch", {"u": 1}, read_only=False)
    guard.reset()
    assert guard.halt is None and guard.config == HARD
    assert guard.before("fetch", {"u": 1}, read_only=False).runs
