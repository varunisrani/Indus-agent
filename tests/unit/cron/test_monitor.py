"""P14-16: monitor mode — hash-gated runs, change blocks, URL limits and failures."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from indus.cron import monitor
from indus.cron.models import CronJob, MonitorState
from indus.cron.monitor import (
    MAX_DIFF_CHARS,
    MAX_URL_BYTES,
    check_monitor,
    snapshot_path,
    split_monitor,
    validate_monitor,
)
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from tests.unit.cron.world import record

URL = "https://status.example.test/build"


def public(host: str, port: int | None) -> Sequence[str]:
    return ["93.184.216.34"]


def private(host: str, port: int | None) -> Sequence[str]:
    return ["10.0.0.5"]


def transport(bodies: list[bytes]) -> Callable[[str | None], httpx.AsyncBaseTransport]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies.pop(0) if len(bodies) > 1 else bodies[0])

    return lambda proxy: httpx.MockTransport(handler)


class FakeModel:
    def __init__(self) -> None:
        self.calls = 0


def tick(scope: Scope, job: CronJob, model: FakeModel, **kw: Any) -> monitor.MonitorOutcome:
    """What the runner does: check, store the new state, call the model only on a change."""
    outcome = asyncio.run(check_monitor(scope, job, **kw))
    if outcome.state is not None:
        job.monitor_state = MonitorState.model_validate(outcome.state)
    if outcome.should_run:
        model.calls += 1
    return outcome


def url_job() -> CronJob:
    return record(id="feedfacecafe", prompt="report", monitor_url=URL)


def test_same_body_twice_suppresses_the_second_run(scope: Scope) -> None:
    job, model = url_job(), FakeModel()
    first = tick(scope, job, model, resolver=public, inner_factory=transport([b"green\n"]))
    assert first.status == "first" and "first look" in (first.block or "")
    assert snapshot_path(scope, job.id).read_bytes() == b"green\n"
    second = tick(scope, job, model, resolver=public, inner_factory=transport([b"green\n"]))
    assert second.status == "no_change" and second.block is None
    assert model.calls == 1


def test_changed_body_injects_a_bounded_diff(scope: Scope) -> None:
    job, model = url_job(), FakeModel()
    old = "\n".join(f"line {n}" for n in range(3000)).encode()
    new = "\n".join(f"line {n} changed" for n in range(3000)).encode()
    tick(scope, job, model, resolver=public, inner_factory=transport([old]))
    changed = tick(scope, job, model, resolver=public, inner_factory=transport([new]))
    assert changed.status == "changed" and model.calls == 2
    block = changed.block or ""
    diff = block.split("```diff\n", 1)[1].split("\n```", 1)[0]
    assert len(diff) <= MAX_DIFF_CHARS and diff.startswith("--- last run")
    shown = block.split("### Output now\n\n```\n", 1)[1].rsplit("\n```", 1)[0]
    assert len(shown) <= monitor.MAX_OUTPUT_CHARS


def test_oversized_body_is_an_error(scope: Scope) -> None:
    job, model = url_job(), FakeModel()
    big = tick(scope, job, model, resolver=public, inner_factory=transport([b"x" * 300 * 1024]))
    assert big.status == "error" and "256 KiB" in (big.error or "")
    exact = tick(
        scope, job, model, resolver=public, inner_factory=transport([b"x" * MAX_URL_BYTES])
    )
    assert exact.status == "first"


def test_private_url_is_refused(scope: Scope) -> None:
    job, model = url_job(), FakeModel()
    outcome = tick(scope, job, model, resolver=private, inner_factory=transport([b"x"]))
    assert outcome.status == "error" and "refused" in (outcome.error or "")
    assert model.calls == 0 and job.monitor_state is None


def test_http_error_is_an_error(scope: Scope) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    job = url_job()
    outcome = asyncio.run(
        check_monitor(
            scope, job, resolver=public, inner_factory=lambda p: httpx.MockTransport(handler)
        )
    )
    assert outcome.status == "error" and "503" in (outcome.error or "")


def test_failing_script_leaves_hash_and_snapshot(scope: Scope) -> None:
    results: list[tuple[bool, str]] = [(True, "v1\n"), (False, "exit 3: disk full"), (True, "v1\n")]

    async def runner(scope: Scope, script: str, job: Any) -> tuple[bool, str]:
        assert script == "check.sh"
        return results.pop(0)

    job = record(id="0123456789ab", prompt="p", monitor_script="check.sh")
    model = FakeModel()
    tick(scope, job, model, run_script=runner)
    stored = job.monitor_state.model_dump() if job.monitor_state else {}
    failed = tick(scope, job, model, run_script=runner)
    assert failed.status == "error" and "disk full" in (failed.error or "")
    assert job.monitor_state is not None and job.monitor_state.model_dump() == stored
    assert snapshot_path(scope, job.id).read_text() == "v1\n"
    assert tick(scope, job, model, run_script=runner).status == "no_change"
    assert model.calls == 1


def test_script_monitor_without_a_runner_is_an_error(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor, "RUN_SCRIPT", None)
    job = record(id="0123456789ab", prompt="p", monitor_script="check.sh")
    outcome = asyncio.run(check_monitor(scope, job))
    assert outcome.status == "error" and "script runner" in (outcome.error or "")


def test_script_monitor_uses_the_cron_script_runner(scope: Scope) -> None:
    scope = dataclasses.replace(scope, clock=SystemClock())  # a real child process
    folder = scope.home / "scripts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "check.sh").write_text("echo build 812 red\n", encoding="utf-8")
    job = record(id="0123456789ab", prompt="p", monitor_script="check.sh")
    first = asyncio.run(check_monitor(scope, job))
    assert first.status == "first" and "build 812 red" in (first.block or "")
    job.monitor_state = MonitorState.model_validate(first.state)
    assert asyncio.run(check_monitor(scope, job)).status == "no_change"
    (folder / "gone.sh").unlink(missing_ok=True)
    missing = record(id="0123456789ab", prompt="p", monitor_script="gone.sh")
    assert asyncio.run(check_monitor(scope, missing)).status == "error"


@pytest.mark.parametrize(
    ("script", "url", "no_agent", "problem"),
    [
        (None, None, False, None),
        ("a.sh", None, False, None),
        (None, URL, False, None),
        ("a.sh", URL, False, "not both"),
        ("a.sh", None, True, "script-only"),
        (None, "ftp://x/y", False, "http"),
    ],
)
def test_validation(
    script: str | None, url: str | None, no_agent: bool, problem: str | None
) -> None:
    found = validate_monitor(script, url, no_agent=no_agent)
    assert (found is None) if problem is None else (problem in (found or ""))


def test_split_monitor() -> None:
    assert split_monitor(URL) == (None, URL)
    assert split_monitor(" check.sh ") == ("check.sh", None)
    assert split_monitor("") == (None, None)
