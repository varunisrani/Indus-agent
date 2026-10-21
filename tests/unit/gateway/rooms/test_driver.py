"""P24-06: the room driver — leases, fenced tasks, turn timing and stop directives."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.foundation.scope import Scope
from indus.gateway.rooms.driver import (
    MAX_PROMPT_BYTES,
    MAX_RESULT_BYTES,
    MAX_TERMINAL_TASKS,
    PRUNE_BATCH,
    SILENCE_LIMIT_S,
    TERMINAL_RETENTION_S,
    TURN_LIMIT_S,
    BadTransition,
    DriverError,
    DriverLease,
    LeaseHeld,
    RoomDriver,
    StaleLease,
    StaleTask,
    TaskConflict,
    TaskKey,
    stop_directives,
)
from indus.state.rooms import RoomStore
from tests.fakes.clock import FakeClock

TTL = 30.0


@pytest.fixture
def store(scope: Scope) -> Iterator[RoomStore]:
    s = RoomStore(scope)
    s.create_room("room", "Planning", [{"profile": "alice"}, {"profile": "bob"}])
    yield s
    s.close()


@pytest.fixture
def driver(store: RoomStore) -> RoomDriver:
    return RoomDriver(store)


def _key(n: int) -> TaskKey:
    return TaskKey("room", f"task-{n}", "thread-1", f"turn-{n}")


def _admit(driver: RoomDriver, n: int, seq: int | None = None, target: str = "bob") -> TaskKey:
    key = _key(n)
    driver.admit(key, {"target": target, "prompt": f"turn {n}", "source_seq": seq or n})
    return key


def _lease(driver: RoomDriver, proc: str = "proc-a", ttl: float = TTL) -> DriverLease:
    return driver.acquire_lease("room", process_generation=proc, ttl_seconds=ttl)


def test_limits_match_the_spec() -> None:
    assert MAX_PROMPT_BYTES == 128 * 1024
    assert MAX_RESULT_BYTES == 256 * 1024
    assert TERMINAL_RETENTION_S == 30 * 86_400
    assert (MAX_TERMINAL_TASKS, PRUNE_BATCH) == (2_048, 1_000)
    assert (TURN_LIMIT_S, SILENCE_LIMIT_S) == (3 * 3_600, 180)


# -- leases ---------------------------------------------------------------------------------------


def test_second_process_cannot_take_a_live_lease(driver: RoomDriver) -> None:
    first = _lease(driver)
    assert (first.lease_generation, first.reclaimed) == (1, False)
    with pytest.raises(LeaseHeld):
        _lease(driver, "proc-b")
    again = _lease(driver)  # the same process renews its own lease
    assert again.lease_generation == 1


def test_takeover_after_expiry_bumps_the_generation(
    driver: RoomDriver, fake_clock: FakeClock
) -> None:
    first = _lease(driver)
    fake_clock.advance(TTL)
    second = _lease(driver, "proc-b")
    assert (second.lease_generation, second.reclaimed) == (2, True)
    with pytest.raises(StaleLease):
        driver.renew_lease(first, ttl_seconds=TTL)
    renewed = driver.renew_lease(second, ttl_seconds=TTL * 2)
    assert renewed.expires_at > second.expires_at


def test_release_is_exact_and_refused_while_running(driver: RoomDriver) -> None:
    lease = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, lease, expected_cancel_gen=0)
    with pytest.raises(BadTransition):
        driver.release_lease(lease)
    driver.settle(attempt, settlement_id="s1", status="settled", result={"text": "ok"})
    assert driver.release_lease(lease) is True
    assert driver.release_lease(lease) is False
    taken = _lease(driver, "proc-b")  # a released lease is free at once
    assert taken.lease_generation == 2


def test_a_lease_needs_the_room_authority(store: RoomStore, driver: RoomDriver) -> None:
    store.claim_authority(
        "room", expected=store.gateway_id, expected_epoch=1, new="gw-elsewhere", event_id="c"
    )
    with pytest.raises(Exception, match="authority"):
        _lease(driver)


def test_ttl_must_be_positive_and_finite(driver: RoomDriver) -> None:
    for bad in (0, -1, float("inf"), float("nan")):
        with pytest.raises(DriverError):
            driver.acquire_lease("room", process_generation="p", ttl_seconds=bad)


# -- admission and order ----------------------------------------------------------------------


def test_admit_is_idempotent_and_unique_per_turn(driver: RoomDriver) -> None:
    key = _admit(driver, 1)
    assert driver.admit(key, {"target": "bob", "prompt": "turn 1", "source_seq": 1}).idempotent
    with pytest.raises(TaskConflict):
        driver.admit(key, {"target": "bob", "prompt": "other", "source_seq": 1})
    clash = TaskKey("room", "task-x", "thread-1", "turn-1")
    with pytest.raises(TaskConflict):
        driver.admit(clash, {"target": "bob", "prompt": "turn 1", "source_seq": 1})


def test_admit_checks_the_prompt_bound(driver: RoomDriver) -> None:
    driver.admit(_key(1), {"target": "bob", "prompt": "p" * MAX_PROMPT_BYTES, "source_seq": 1})
    with pytest.raises(DriverError, match="larger"):
        driver.admit(
            _key(2), {"target": "bob", "prompt": "p" * (MAX_PROMPT_BYTES + 1), "source_seq": 2}
        )
    with pytest.raises(DriverError, match="unknown task fields"):
        driver.admit(_key(3), {"target": "bob", "prompt": "x", "source_seq": 3, "extra": 1})
    with pytest.raises(DriverError, match="non-empty"):
        driver.admit(_key(4), {"target": "bob", "prompt": "  ", "source_seq": 4})


def test_start_takes_only_the_head_and_waits_for_recovery(
    driver: RoomDriver, fake_clock: FakeClock
) -> None:
    lease = _lease(driver)
    second = _admit(driver, 2, seq=5)
    first = _admit(driver, 1, seq=3)
    with pytest.raises(BadTransition, match="not next"):
        driver.start(second, lease, expected_cancel_gen=0)
    attempt = driver.start(first, lease, expected_cancel_gen=0)
    with pytest.raises(BadTransition, match="in flight"):
        driver.start(second, lease, expected_cancel_gen=0)
    # the process dies; a new one takes over and finds the running task
    fake_clock.advance(TTL)
    other = _lease(driver, "proc-b")
    with pytest.raises(BadTransition, match="in flight or awaiting recovery"):
        driver.start(second, other, expected_cancel_gen=0)
    found = driver.recover(other)
    assert found == {"queued": [second], "indeterminate": [first]}
    with pytest.raises(BadTransition):  # still blocked until the operator decides
        driver.start(second, other, expected_cancel_gen=0)
    driver.defer_indeterminate(
        first, other, expected_exec_gen=attempt.exec_gen, expected_cancel_gen=0, reason="lost"
    )
    assert driver.start(second, other, expected_cancel_gen=0).exec_gen == 1


# -- fenced settlement ----------------------------------------------------------------------


def test_settle_is_idempotent_and_conflicts_on_other_results(driver: RoomDriver) -> None:
    lease = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, lease, expected_cancel_gen=0)
    done = driver.settle(attempt, settlement_id="s1", status="settled", result={"text": "hi"})
    assert (done.status, done.result) == ("settled", {"text": "hi"})
    again = driver.settle(attempt, settlement_id="s1", status="settled", result={"text": "hi"})
    assert again.idempotent
    with pytest.raises(TaskConflict):
        driver.settle(attempt, settlement_id="s2", status="failed", result={"error": "x"})
    with pytest.raises(DriverError, match="larger"):
        driver.settle(
            attempt, settlement_id="s3", status="settled", result={"t": "r" * MAX_RESULT_BYTES}
        )


def test_a_task_whose_lease_generation_changed_settles_indeterminate(
    driver: RoomDriver, fake_clock: FakeClock
) -> None:
    old = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, old, expected_cancel_gen=0)
    fake_clock.advance(TTL)
    new = _lease(driver, "proc-b")
    assert new.lease_generation == old.lease_generation + 1
    driver.recover(new)
    assert driver.get_task(key).status == "indeterminate"
    with pytest.raises((StaleTask, StaleLease)):
        driver.settle(attempt, settlement_id="late", status="settled", result={"text": "late"})
    # recovery again never requeues it
    assert driver.recover(new)["queued"] == []
    assert driver.get_task(key).status == "indeterminate"
    # only an explicit operator requeue runs it again
    requeued = driver.requeue_indeterminate(
        key, new, expected_exec_gen=attempt.exec_gen, expected_cancel_gen=0
    )
    assert requeued.status == "queued"
    rerun = driver.start(key, new, expected_cancel_gen=0)
    assert rerun.exec_gen == attempt.exec_gen + 1


def test_old_attempt_cannot_settle_even_before_recovery(
    driver: RoomDriver, fake_clock: FakeClock
) -> None:
    old = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, old, expected_cancel_gen=0)
    fake_clock.advance(TTL)
    _lease(driver, "proc-b")
    with pytest.raises(StaleLease):
        driver.settle(attempt, settlement_id="late", status="settled", result={})
    assert driver.get_task(key).status == "running"


def test_resolve_indeterminate_with_a_verified_result(
    driver: RoomDriver, fake_clock: FakeClock
) -> None:
    old = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, old, expected_cancel_gen=0)
    fake_clock.advance(TTL)
    new = _lease(driver, "proc-b")
    driver.recover(new)
    with pytest.raises(StaleTask):
        driver.resolve_indeterminate(
            key, new, expected_exec_gen=attempt.exec_gen + 1, expected_cancel_gen=0,
            settlement_id="v", status="settled", result={},
        )  # fmt: skip
    resolved = driver.resolve_indeterminate(
        key, new, expected_exec_gen=attempt.exec_gen, expected_cancel_gen=0,
        settlement_id="v", status="settled", result={"text": "verified"},
    )  # fmt: skip
    assert resolved.status == "settled"


def test_cancel_indeterminate(driver: RoomDriver, fake_clock: FakeClock) -> None:
    old = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, old, expected_cancel_gen=0)
    fake_clock.advance(TTL)
    new = _lease(driver, "proc-b")
    driver.recover(new)
    done = driver.cancel_indeterminate(
        key, new, expected_exec_gen=attempt.exec_gen, expected_cancel_gen=0, cancel_id="c1"
    )
    assert (done.status, done.cancel_gen) == ("cancelled", 1)


# -- cancellation and stop --------------------------------------------------------------------


def test_cancel_queued_task(driver: RoomDriver) -> None:
    key = _admit(driver, 1)
    done = driver.cancel_queued(key, cancel_id="c1", expected_cancel_gen=0)
    assert (done.status, done.cancel_gen) == ("cancelled", 1)
    assert driver.cancel_queued(key, cancel_id="c1", expected_cancel_gen=0).idempotent
    with pytest.raises(BadTransition):
        driver.cancel_queued(key, cancel_id="c2", expected_cancel_gen=1)


def test_running_work_needs_a_two_phase_stop(driver: RoomDriver) -> None:
    lease = _lease(driver)
    key = _admit(driver, 1)
    driver.start(key, lease, expected_cancel_gen=0)
    with pytest.raises(BadTransition, match="two-phase"):
        driver.cancel_queued(key, cancel_id="c1", expected_cancel_gen=0)


def test_two_phase_stop(driver: RoomDriver) -> None:
    lease = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, lease, expected_cancel_gen=0)
    stopping = driver.begin_stop(key, cancel_id="stop-1", expected_cancel_gen=0)
    assert (stopping.status, stopping.cancel_gen) == ("stopping", 1)
    assert driver.begin_stop(key, cancel_id="stop-1", expected_cancel_gen=0).idempotent
    with pytest.raises(StaleTask):  # the running attempt lost its fence
        driver.settle(attempt, settlement_id="s", status="settled", result={})
    with pytest.raises(StaleTask):
        driver.complete_stop(key, cancel_id="other", expected_cancel_gen=1)
    done = driver.complete_stop(key, cancel_id="stop-1", expected_cancel_gen=1)
    assert done.status == "cancelled"
    assert driver.complete_stop(key, cancel_id="stop-1", expected_cancel_gen=1).idempotent


def test_a_result_that_wins_the_stop_race_is_kept(driver: RoomDriver) -> None:
    lease = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, lease, expected_cancel_gen=0)
    driver.begin_stop(key, cancel_id="stop-1", expected_cancel_gen=0)
    kept = driver.settle_stopping(
        key, lease, expected_exec_gen=attempt.exec_gen, expected_cancel_gen=1,
        settlement_id="won", status="settled", result={"text": "finished first"},
    )  # fmt: skip
    assert kept.status == "settled"


# -- turn timing ------------------------------------------------------------------------------


def test_silent_member_expires_at_three_minutes(driver: RoomDriver, fake_clock: FakeClock) -> None:
    lease = _lease(driver, ttl=TURN_LIMIT_S * 2)
    key = _admit(driver, 1)
    driver.start(key, lease, expected_cancel_gen=0)
    fake_clock.advance(SILENCE_LIMIT_S - 1)
    assert driver.expire_turns(lease) == []
    fake_clock.advance(1)
    assert driver.expire_turns(lease) == [(key, "member_silent")]
    task = driver.get_task(key)
    assert (task.status, task.result) == ("failed", {"reason": "member_silent"})


def test_working_member_is_kept_to_three_hours(driver: RoomDriver, fake_clock: FakeClock) -> None:
    lease = _lease(driver, ttl=TURN_LIMIT_S * 2)
    key = _admit(driver, 1)
    attempt = driver.start(key, lease, expected_cancel_gen=0)
    step = SILENCE_LIMIT_S - 10
    elapsed = 0.0
    while elapsed + step < TURN_LIMIT_S:
        fake_clock.advance(step)
        elapsed += step
        driver.heartbeat(attempt)
        assert driver.expire_turns(lease) == [], f"expired early at {elapsed} s"
    fake_clock.advance(TURN_LIMIT_S - elapsed)
    assert driver.expire_turns(lease) == [(key, "turn_limit")]


def test_heartbeat_of_a_stale_attempt_is_refused(driver: RoomDriver) -> None:
    lease = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, lease, expected_cancel_gen=0)
    driver.begin_stop(key, cancel_id="s", expected_cancel_gen=0)
    with pytest.raises(StaleTask):
        driver.heartbeat(attempt)


# -- retention ----------------------------------------------------------------------------------


def test_prune_terminal_by_age_and_count(driver: RoomDriver, fake_clock: FakeClock) -> None:
    for n in range(1, 6):
        key = _admit(driver, n)
        driver.cancel_queued(key, cancel_id=f"c{n}", expected_cancel_gen=0)
        fake_clock.advance(1)
    assert driver.prune_terminal("room", keep=3) == 2
    assert {t.key.task_id for t in driver.list_tasks("room")} == {"task-3", "task-4", "task-5"}
    fake_clock.advance(TERMINAL_RETENTION_S)
    assert driver.prune_terminal("room") == 3
    assert driver.list_tasks("room") == []


def test_prune_leaves_live_tasks(driver: RoomDriver, fake_clock: FakeClock) -> None:
    _admit(driver, 1)
    fake_clock.advance(TERMINAL_RETENTION_S * 2)
    assert driver.prune_terminal("room", keep=0) == 0


def test_disbanding_a_room_purges_its_tasks_with_it(
    store: RoomStore, driver: RoomDriver, fake_clock: FakeClock
) -> None:
    _admit(driver, 1)
    _lease(driver)
    store.disband_room("room", authority=store.gateway_id, epoch=1)
    fake_clock.advance(91 * 86_400)
    assert store.prune_disbanded() == 1
    assert store.read(lambda c: c.execute("SELECT COUNT(*) FROM room_tasks").fetchone()[0]) == 0
    assert store.read(lambda c: c.execute("SELECT COUNT(*) FROM room_leases").fetchone()[0]) == 0


# -- stop directives ----------------------------------------------------------------------------

HANDLES = ("bob", "alice")


@pytest.mark.parametrize(
    ("text", "held"),
    [
        ("stop @bob", {"bob"}),
        ("@bob stop", {"bob"}),
        ("@bob please pause", {"bob"}),
        ("@bob, halt!", {"bob"}),
        ("please stop @alice now", {"alice"}),
        ("@bob please just stop now", set()),
        ("I will stop by later, @bob", set()),
        ("@bob can you explain why the bus had to stop?", set()),
        ("stop @all", {"bob", "alice"}),
        ("@everyone pause", {"bob", "alice"}),
        ("stop @carol", set()),
        ("`stop @bob`", set()),
        ('he said "stop @bob" yesterday', set()),
        ("> stop @bob\n@alice what do you think?", set()),
        ("```\nstop @bob\n```\n@alice go on", set()),
        ("~~~\n@bob stop\n~~~", set()),
        ("look: `x` @bob stop", {"bob"}),
    ],
)
def test_stop_directives(text: str, held: set[str]) -> None:
    assert stop_directives(text, HANDLES) == held


def test_stop_detection_can_be_turned_off() -> None:
    assert stop_directives("stop @bob", HANDLES, enabled=False) == frozenset()


# -- compare-step additions -----------------------------------------------------------------------


def test_two_contenders_have_one_winner(scope: Scope) -> None:
    import dataclasses
    import threading

    from indus.foundation.clock import SystemClock

    real = dataclasses.replace(scope, clock=SystemClock())
    path = scope.root / "contended-rooms.db"
    store = RoomStore(real, path)
    store.create_room("room", "Planning", [{"profile": "bob"}])
    other_store = RoomStore(real, path)
    drivers = [RoomDriver(store), RoomDriver(other_store)]
    results: list[str] = []

    def take(driver: RoomDriver, proc: str) -> None:
        try:
            driver.acquire_lease("room", process_generation=proc, ttl_seconds=TTL)
            results.append("won")
        except LeaseHeld:
            results.append("held")

    try:
        threads = [
            threading.Thread(target=take, args=(drivers[0], "p1")),
            threading.Thread(target=take, args=(drivers[1], "p2")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(results) == ["held", "won"]
    finally:
        other_store.close()
        store.close()


def test_authority_transfer_fences_the_lease_and_a_late_settlement(
    store: RoomStore, driver: RoomDriver
) -> None:
    from indus.state.rooms import RoomAuthorityConflict

    lease = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, lease, expected_cancel_gen=0)
    store.claim_authority(
        "room", expected=store.gateway_id, expected_epoch=1, new="gw-new", event_id="c"
    )
    with pytest.raises(RoomAuthorityConflict):
        driver.settle(attempt, settlement_id="late", status="settled", result={})
    with pytest.raises(RoomAuthorityConflict):
        driver.renew_lease(lease, ttl_seconds=TTL)


def test_disbanding_fences_lease_operations(store: RoomStore, driver: RoomDriver) -> None:
    from indus.state.rooms import RoomNotFound

    lease = _lease(driver)
    key = _admit(driver, 1)
    store.disband_room("room", authority=store.gateway_id, epoch=1)
    with pytest.raises(RoomNotFound):
        driver.start(key, lease, expected_cancel_gen=0)
    with pytest.raises(RoomNotFound):
        driver.admit(_key(2), {"target": "bob", "prompt": "x", "source_seq": 2})


def test_proven_not_admitted_attempt_returns_to_the_queue(
    driver: RoomDriver, fake_clock: FakeClock
) -> None:
    lease = _lease(driver)
    key = _admit(driver, 1)
    attempt = driver.start(key, lease, expected_cancel_gen=0)
    back = driver.requeue_not_admitted(attempt)
    assert (back.status, back.run_fence, back.exec_gen) == ("queued", None, attempt.exec_gen)
    assert driver.requeue_not_admitted(attempt).idempotent
    second = driver.start(key, lease, expected_cancel_gen=0)
    assert second.exec_gen == attempt.exec_gen + 1
    with pytest.raises(StaleTask):  # the older attempt cannot put the new one back
        driver.requeue_not_admitted(attempt)


def test_renewal_never_shortens_a_lease(driver: RoomDriver) -> None:
    lease = driver.acquire_lease("room", process_generation="p", ttl_seconds=600)
    shorter = driver.renew_lease(lease, ttl_seconds=5)
    assert shorter.expires_at == lease.expires_at


def test_a_tampered_task_row_is_not_used(store: RoomStore, driver: RoomDriver) -> None:
    key = _admit(driver, 1)
    store.write(
        lambda c: c.execute(
            "UPDATE room_tasks SET payload = ? WHERE task_id = ?",
            ('{"prompt":"other","source_seq":1,"target":"bob"}', key.task_id),
        )
    )
    with pytest.raises(DriverError, match="digest"):
        driver.get_task(key)
