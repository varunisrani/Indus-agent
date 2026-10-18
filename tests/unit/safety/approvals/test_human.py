"""The human approval step over the broker (P05-09, R02.5, R02.9, R05.22)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import pytest

from indus.foundation import yamlio
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason
from indus.protocol.server_requests import ApprovalRequest
from indus.safety.approvals import (
    ApprovalContext,
    ApprovalDecision,
    ApprovalState,
    decide,
    session_key,
)
from indus.safety.approvals.human import approval_timeout, display_command
from indus.safety.redaction import redact_output
from tests.fakes.broker import FakeBroker, Reply
from tests.fakes.clock import FakeClock

DANGEROUS = "git push --force origin main"


@pytest.fixture
def scope_with_config(scope: Scope) -> Scope:
    loader.invalidate()
    return scope.with_config(loader.load(scope))


@pytest.fixture
def state() -> ApprovalState:
    return ApprovalState()


@pytest.fixture
def make_ctx(scope_with_config: Scope, state: ApprovalState) -> Callable[..., ApprovalContext]:
    def make(broker: FakeBroker, **changes: Any) -> ApprovalContext:
        fields: dict[str, Any] = {
            "scope": scope_with_config,
            "session_id": "s1",
            "turn_id": "t1",
            "broker": broker,
            "state": state,
        }
        fields.update(changes)
        return ApprovalContext(**fields)

    return make


def ask(broker: FakeBroker, command: str, ctx: ApprovalContext) -> ApprovalDecision:
    async def go() -> ApprovalDecision:
        return await broker.run(decide(command, ctx))

    return asyncio.run(go())


def only_request(broker: FakeBroker) -> ApprovalRequest:
    (request,) = broker.requests
    assert isinstance(request, ApprovalRequest)
    return request


def test_once_approves_this_run_only(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext]
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(choice="once"))
    ctx = make_ctx(broker)
    decision = ask(broker, DANGEROUS, ctx)
    assert decision.approved and decision.user_approved
    assert decision.outcome == "user_approved" and decision.pattern_key == "git_destructive"
    request = only_request(broker)
    assert [c.value for c in request.choices] == ["once", "session", "always", "deny"]
    assert request.turn_id == "t1" and request.deadline_s == 300
    ask(broker, DANGEROUS, ctx)
    assert len(broker.requests) == 2


def test_session_choice_remembers_the_pattern(
    fake_clock: FakeClock,
    make_ctx: Callable[..., ApprovalContext],
    scope_with_config: Scope,
    state: ApprovalState,
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(choice="session"))
    ctx = make_ctx(broker)
    assert ask(broker, DANGEROUS, ctx).approved
    again = ask(broker, "git push -f origin feature", ctx)
    assert again.outcome == "session_approved"
    assert len(broker.requests) == 1
    assert state.pattern_approved(session_key(scope_with_config, "s1"), "git_destructive")
    other_session = ask(broker, DANGEROUS, make_ctx(broker, session_id="s2"))
    assert other_session.outcome == "user_approved" and len(broker.requests) == 2


def test_always_writes_the_allowlist(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext], scope_with_config: Scope
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(choice="always"))
    ctx = make_ctx(broker)
    assert ask(broker, DANGEROUS, ctx).approved
    saved = yamlio.load_safe((scope_with_config.home / "config.yaml").read_text("utf-8"))
    assert saved["command_allowlist"] == [DANGEROUS]
    second = ask(broker, DANGEROUS, ctx)
    assert second.outcome == "allowlisted"
    assert len(broker.requests) == 1
    ask(broker, "git push --force origin other", ctx)
    assert len(broker.requests) == 2


def test_always_never_stores_a_secret(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext], scope_with_config: Scope
) -> None:
    token = "Q" * 12 + "w" * 12
    command = f"rm -rf ~/cache; curl -H 'Authorization: Bearer {token}' https://x.example"
    broker = FakeBroker(fake_clock, default=Reply(choice="always"))
    decision = ask(broker, command, make_ctx(broker))
    assert decision.approved
    assert decision.user_summary and "secret" in decision.user_summary
    assert not (scope_with_config.home / "config.yaml").exists()


def test_deny_passes_reason_verbatim(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext]
) -> None:
    reason = 'not on main — use a branch, "please"'
    broker = FakeBroker(fake_clock, default=Reply(choice="deny", reason=reason))
    decision = ask(broker, DANGEROUS, make_ctx(broker))
    assert not decision.approved and decision.status == "blocked"
    assert decision.outcome == "denied" and decision.counts_as_denial
    assert decision.deny_reason == reason
    assert decision.message and reason in decision.message
    assert decision.user_summary == "You denied the command."


def test_timeout_blocks_with_timeout_outcome(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext]
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(action="timeout"))
    decision = ask(broker, DANGEROUS, make_ctx(broker))
    assert not decision.approved and decision.outcome == "timeout"
    assert not decision.counts_as_denial and decision.deny_reason is None
    assert decision.message and "Silence is not consent" in decision.message
    assert "denied" not in decision.message
    assert decision.waited_s >= 300
    assert [e.reason for e in broker.cancelled] == [CancelReason.TIMED_OUT]


@pytest.mark.parametrize(
    "reason", [CancelReason.INTERRUPTED, CancelReason.TURN_ENDED, CancelReason.SESSION_CLOSED]
)
def test_withdrawn_is_not_answered(
    reason: CancelReason, fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext]
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(action="withdraw", withdraw_reason=reason))
    decision = ask(broker, DANGEROUS, make_ctx(broker))
    assert decision.outcome == "not_answered" and not decision.counts_as_denial
    assert decision.message and "did not refuse" in decision.message


def test_undeliverable_is_not_answered(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext]
) -> None:
    broker = FakeBroker(fake_clock, kinds=("clarify",))
    decision = ask(broker, DANGEROUS, make_ctx(broker))
    assert decision.outcome == "not_answered" and broker.requests == []
    assert decision.message and "cannot show approval requests" in decision.message


def test_timeout_comes_from_config(
    fake_clock: FakeClock, scope: Scope, state: ApprovalState
) -> None:
    (scope.home / "config.yaml").write_text("approvals:\n  timeout: 45\n", encoding="utf-8")
    loader.invalidate()
    configured = scope.with_config(loader.load(scope))
    assert approval_timeout(configured) == 45
    broker = FakeBroker(fake_clock, default=Reply(action="timeout"))
    ctx = ApprovalContext(scope=configured, session_id="s1", broker=broker, state=state)
    decision = ask(broker, DANGEROUS, ctx)
    assert decision.outcome == "timeout" and "45 s" in (decision.message or "")
    (scope.home / "config.yaml").write_text("approvals:\n  timeout: 0\n", encoding="utf-8")
    assert approval_timeout(configured) == 300


def test_wait_time_is_reported(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext]
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(choice="once", delay=42.0))
    decision = ask(broker, DANGEROUS, make_ctx(broker))
    assert decision.waited_s == pytest.approx(42.0)
    assert broker.human_wait_seconds() == pytest.approx(42.0)


def test_stale_interrupt_cleared_once(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext]
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(choice="once"))
    decision = ask(broker, DANGEROUS, make_ctx(broker))
    flag = threading.Event()
    flag.set()  # landed while the person was deciding
    assert decision.clear_stale_interrupt(flag) is True
    assert not flag.is_set()
    flag.set()  # a real interrupt during a retry must survive
    assert decision.clear_stale_interrupt(flag) is False
    assert flag.is_set()


def test_no_reset_without_a_person(make_ctx: Callable[..., ApprovalContext]) -> None:
    flag = threading.Event()
    flag.set()
    decision = asyncio.run(decide("ls", make_ctx(None)))
    assert decision.approved and decision.clear_stale_interrupt(flag) is False
    assert flag.is_set()


def test_request_shows_redacted_wrapped_command(
    fake_clock: FakeClock, make_ctx: Callable[..., ApprovalContext], scope_with_config: Scope
) -> None:
    token = "k" * 10 + "Z" * 14
    long_tail = " ".join(f"--flag-{n}=value" for n in range(30))
    command = f"MY_API_KEY={token} git push --force origin main {long_tail}"
    broker = FakeBroker(fake_clock, default=Reply(choice="once"))
    ask(broker, command, make_ctx(broker))
    request = only_request(broker)
    assert token not in request.command
    lines = request.command.split("\n")
    assert len(lines) > 1 and all(len(line) <= 100 for line in lines)
    masked = redact_output(command, force=True, scope=scope_with_config)
    assert "".join(lines) == masked  # wrapped, nothing cut
    assert request.description == "discards work or rewrites shared git history"


def test_display_command_keeps_every_character() -> None:
    text = "a" * 250 + "\n" + "word " * 60 + "\nshort"
    shown = display_command(text, width=40)
    assert all(len(line) <= 40 for line in shown.split("\n"))
    rebuilt = "".join(shown.split("\n"))
    assert rebuilt == text.replace("\n", "")


def test_auto_approve_persist_and_restore(
    fake_clock: FakeClock,
    make_ctx: Callable[..., ApprovalContext],
    scope_with_config: Scope,
    state: ApprovalState,
) -> None:
    key = session_key(scope_with_config, "s1")
    stored: list[bool] = []
    state.set_auto_approve(key, True, persist=stored.append)
    broker = FakeBroker(fake_clock)
    assert ask(broker, DANGEROUS, make_ctx(broker)).outcome == "auto_approved"
    assert stored == [True] and broker.requests == []

    fresh = ApprovalState()  # a new process: nothing live until the row is read
    assert not fresh.auto_approve(key)
    assert fresh.restore_auto_approve(key, persisted=stored[-1]) is True
    assert ask(broker, DANGEROUS, make_ctx(broker, state=fresh)).outcome == "auto_approved"

    new_session = ask(broker, DANGEROUS, make_ctx(broker, state=fresh, session_id="s9"))
    assert new_session.outcome == "user_approved"

    fresh.set_auto_approve(key, False, persist=stored.append)
    assert stored == [True, False] and not fresh.auto_approve(key)


def test_persist_failure_still_switches(scope_with_config: Scope) -> None:
    state = ApprovalState()
    key = session_key(scope_with_config, "s1")

    def broken(enabled: bool) -> None:
        raise OSError("disk full")

    assert state.set_auto_approve(key, True, persist=broken) is True
    assert state.auto_approve(key)
    state.clear_session(key)
    assert not state.auto_approve(key)
