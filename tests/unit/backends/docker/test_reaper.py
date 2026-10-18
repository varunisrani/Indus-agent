"""Sweeping old Indus containers of this profile (P11-07, R02.8, R02.23)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.backends.docker import reaper
from indus.backends.docker.environment import (
    LABEL_AGENT,
    LABEL_PROFILE,
    LABEL_TASK,
    SHARED_TASK,
)
from indus.backends.docker.probe import ClientInfo, DockerCli
from indus.backends.docker.reaper import (
    ReapReport,
    parse_finished_at,
    reap_orphans,
    set_liveness,
    start_reaper_once,
)
from tests.fakes.backend.docker_cli import FakeDocker
from tests.fakes.clock import FakeClock
from tests.unit.backends.docker.conftest import docker_scope

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
OLD = "2026-10-16T08:00:00.123456789Z"  # 52 h before NOW
RECENT = "2026-10-18T02:00:00Z"  # 10 h before NOW


def _labels(task: str = "s1", profile: str = "work") -> dict[str, str]:
    return {LABEL_AGENT: "1", LABEL_PROFILE: profile, LABEL_TASK: task}


def _cli(fake: FakeDocker) -> DockerCli:
    return DockerCli(ClientInfo(str(fake.bin_dir / "docker"), "docker"), {}, runner=fake.runner)


def _sweep(fake: FakeDocker, clock: FakeClock, **kwargs: object) -> ReapReport:
    options: dict[str, object] = {"profile_label": "work", "clock": clock}
    options.update(kwargs)
    return asyncio.run(reap_orphans(_cli(fake), **options))  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


def test_finished_at_parsing() -> None:
    assert parse_finished_at(OLD) == datetime(2026, 10, 16, 8, 0, 0, 123456, tzinfo=UTC)
    assert parse_finished_at("0001-01-01T00:00:00Z") is None
    assert parse_finished_at("yesterday") is None
    assert parse_finished_at("") is None


def test_only_stale_labelled_containers_of_this_profile(
    fake_docker: FakeDocker, clock: FakeClock
) -> None:
    stale = fake_docker.add_container(labels=_labels(), state_name="exited", finished_at=OLD)
    fresh = fake_docker.add_container(labels=_labels(), state_name="exited", finished_at=RECENT)
    other_profile = fake_docker.add_container(
        labels=_labels(profile="home"), state_name="exited", finished_at=OLD
    )
    unlabelled = fake_docker.add_container(
        labels={"app": "web"}, state_name="exited", finished_at=OLD
    )
    shared = fake_docker.add_container(labels=_labels(task=SHARED_TASK))
    report = _sweep(fake_docker, clock)
    left = set(fake_docker.containers())
    assert stale not in left
    assert {fresh, other_profile, unlabelled, shared} <= left
    assert report.removed == [stale[:12]]
    rm = [c for c in fake_docker.commands() if c[0] == "rm"]
    assert rm == [["rm", stale[:12]]]  # plain rm for a stopped one


def test_zero_finished_at_is_kept(fake_docker: FakeDocker, clock: FakeClock) -> None:
    never = fake_docker.add_container(labels=_labels(), state_name="created")
    _sweep(fake_docker, clock)
    assert never in fake_docker.containers()


def test_running_orphan_needs_session_and_jobs_gone(
    fake_docker: FakeDocker, clock: FakeClock
) -> None:
    gone = fake_docker.add_container(labels=_labels(task="s-gone"))
    busy = fake_docker.add_container(labels=_labels(task="s-busy"))
    open_one = fake_docker.add_container(labels=_labels(task="s-open"))
    _sweep(
        fake_docker,
        clock,
        session_alive=lambda task: task == "s-open",
        jobs_alive=lambda task: task == "s-busy",
    )
    left = set(fake_docker.containers())
    assert gone not in left and {busy, open_one} <= left
    assert ["rm", "-f", gone[:12]] in fake_docker.commands()


def test_running_containers_kept_until_the_host_answers(
    fake_docker: FakeDocker, clock: FakeClock
) -> None:
    running = fake_docker.add_container(labels=_labels(task="s1"))
    _sweep(fake_docker, clock)
    assert running in fake_docker.containers()
    set_liveness(session_alive=lambda _: False)
    _sweep(fake_docker, clock)
    assert running not in fake_docker.containers()


def test_kept_ids_are_never_removed(fake_docker: FakeDocker, clock: FakeClock) -> None:
    mine = fake_docker.add_container(labels=_labels(), state_name="exited", finished_at=OLD)
    _sweep(fake_docker, clock, keep={mine})
    assert mine in fake_docker.containers()


def test_budget_exceeded_stops_and_logs(
    fake_docker: FakeDocker, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    for _ in range(3):
        fake_docker.add_container(labels=_labels(), state_name="exited", finished_at=OLD)

    def slow(argv: Sequence[str]) -> None:
        if argv[1] == "inspect":
            clock.advance(20)  # each inspect "takes" 20 s

    fake_docker.on_call = slow
    caplog.set_level(logging.WARNING)
    report = _sweep(fake_docker, clock)
    assert report.budget_exceeded
    assert len(report.removed) == 2 and len(fake_docker.containers()) == 1
    assert "sweep stopped after 30 s; 1 container(s) left" in caplog.text


def test_listing_failure_is_quiet(fake_docker: FakeDocker, clock: FakeClock) -> None:
    fake_docker.set_daemon(False)
    assert _sweep(fake_docker, clock) == ReapReport()


def test_once_per_process(tmp_path: Path, fake_docker: FakeDocker, clock: FakeClock) -> None:
    stale = fake_docker.add_container(labels=_labels(), state_name="exited", finished_at=OLD)
    scope = docker_scope(tmp_path, fake_docker, clock)
    cli = _cli(fake_docker)

    async def go() -> tuple[ReapReport | None, object]:
        first = start_reaper_once(cli, scope, profile_label="work")
        second = start_reaper_once(cli, scope, profile_label="work")
        assert first is not None
        return await first, second

    report, second = asyncio.run(go())
    assert second is None
    assert report is not None and report.removed == [stale[:12]]
    reaper.reset_for_tests()
