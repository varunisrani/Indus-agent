"""P14 "Done when" (M4, second half) end to end on the fake model and the fake clock.

`/cron add "every 30m" check the build` and `cronjob_manage` create jobs in the real store; the
real ticker (through the host's backend, the same one `indus cron tick` uses) fires them with the
real agent runner against the fake model server; each run writes
`<home>/cron/output/<id>/<ts>.md` and delivers locally; `[SILENT]` stays silent; `[JOB_FAILED]` is
delivered, counts toward the failure streak and opens one incident that holds the repeats; a job
with a missing key is blocked once with no model call; pause/resume and downtime catch up with one
run; an attempt whose process died mid-run is `unknown` after reaping and never re-run. A second
test runs a cron job late inside the repeated fall-back hour and checks it fires once.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from click.testing import CliRunner

from indus.cli.args import CliState
from indus.cli.commands import cron as cron_cli
from indus.cli.slash.cron_cmds import cron_cmd as slash_cron
from indus.core.persist import MemoryStore
from indus.cron import delivery, ports, store, tool
from indus.cron.context import CronContext
from indus.cron.delivery import DeliveryReceipt, DeliveryRequest
from indus.cron.incidents import IncidentLedger
from indus.cron.ledger import ExecutionLedger
from indus.cron.models import CronJob
from indus.cron.output import documents, read_document
from indus.cron.runner import AgentRunRequest, AgentRunResult
from indus.cron.ticker import Ticker, TickReport
from indus.foundation.scope import Scope
from indus.service.cron_host import CronAgentRunner, CronHost
from tests.fakes.agent import DictConfig, any_window, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn
from tests.fakes.pids import FakePids
from tests.unit.cli.slash._fakes import make_ctx

ME = (100, "boot:100")
DEAD = 4242


class Demo:
    """One profile with the real cron backend; the agent runs go to per-job fake model servers."""

    def __init__(self, scope: Scope, clock: FakeClock, pids: FakePids) -> None:
        self.scope = scope
        self.clock = clock
        self.pids = pids
        self.servers: dict[str, FakeModelServer] = {}
        self.sent: list[DeliveryRequest] = []

    def host(self, scope: Scope | None = None) -> CronHost:
        return CronHost(scope or self.scope, agent=self.agent, procs=self.pids)

    def serve(self, job_id: str, *replies: str) -> FakeModelServer:
        server = FakeModelServer(script_of(*(text_turn(r) for r in replies)), clock=self.clock)
        self.servers[job_id] = server
        return server

    async def agent(self, request: AgentRunRequest) -> AgentRunResult:
        server = self.servers[request.job.id]
        runner = CronAgentRunner(
            self.scope,
            route=lambda _scope, _choice: fake_route(server),
            client_factory=lambda _route: server.client(),
            list_models=any_window,
            store=MemoryStore(),
        )
        return await runner(request)

    def tick(self) -> TickReport:
        host = self.host()
        with host.opened() as ctx:
            ticker = Ticker(host.env(ctx))
            return asyncio.run(run_driven(ticker.tick(wait=True), self.clock, step=0.5))

    def job(self, job_id: str) -> CronJob:
        found = self.host().get(job_id)
        assert found is not None
        return found

    def attempts(self, job_id: str) -> list[Any]:
        with self.host().opened() as ctx:
            return list(reversed(ctx.ledger.history(job_id, 100)))

    def advance_to(self, when: datetime) -> None:
        gap = (when - self.clock.now()).total_seconds()
        if gap > 0:
            self.clock.advance(gap)

    def sent_for(self, job_id: str) -> list[DeliveryRequest]:
        return [r for r in self.sent if r.job_id == job_id]

    def calls(self, job_id: str) -> int:
        server = self.servers.get(job_id)
        return 0 if server is None else len(server.model_requests)


@pytest.fixture
def demo(scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> Iterator[Demo]:
    key = "sk-" + "demo-" + "0123456789abcdef" * 2  # built at run time (no literal)
    env = scope.env.with_overlay({"OPENAI_API_KEY": key, "ANTHROPIC_API_KEY": None})
    config = DictConfig(
        {"model": {"provider": "openai", "default": "gpt-5"}, "cron": {"max_parallel_jobs": 1}}
    )
    scoped = scope.with_env(env).with_config(config)
    found = Demo(scoped, fake_clock, FakePids(ME).spawn(DEAD))
    monkeypatch.setattr(ports, "BACKEND_FACTORY", lambda s: found.host(s))
    local = delivery.deliverer_for("local")
    assert local is not None

    async def capture(request: DeliveryRequest) -> DeliveryReceipt:
        found.sent.append(request)
        return await local(request)

    delivery.register_deliverer("local", capture)
    try:
        yield found
    finally:
        delivery.register_deliverer("local", local)


def cli(scope: Scope, *argv: str) -> tuple[int, str, str]:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(cron_cli.cron_group, list(argv), obj=state)
    return result.exit_code, result.stdout, result.stderr


def only(jobs: list[CronJob], pick: Callable[[CronJob], bool]) -> CronJob:
    (found,) = [j for j in jobs if pick(j)]
    return found


# Job ids are random and jobs due in the same slot dispatch in id order, so with
# `max_parallel_jobs: 1` the draw decides whether the missing-key job runs before or after the
# build check. Both orders are pinned (ids handed out in creation order: build, inbox, missing key).
DISPATCH_ORDERS = {
    "missing_key_first": ("b0000000000b", "c0000000000c", "a0000000000a"),
    "missing_key_last": ("a0000000000a", "b0000000000b", "c0000000000c"),
}


@pytest.mark.parametrize("order", sorted(DISPATCH_ORDERS))
def test_m4_cron_demo(demo: Demo, order: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(DISPATCH_ORDERS[order])
    monkeypatch.setattr(store, "new_job_id", lambda: next(ids))
    scope, clock = demo.scope, demo.clock
    start = clock.now()

    # -- create: `/cron add` (CLI slash), `cronjob_manage` (the agent), `indus cron create` -------
    ctx = make_ctx(scope)
    slash_cron(ctx, 'add "every 30m" check the build')
    assert any("created" in line for line in ctx.lines), ctx.lines
    made = asyncio.run(
        tool.cronjob_manage(
            {"action": "create", "schedule": "every 1h", "prompt": "summarise the inbox"},
            scope=scope,
            session_id="chat-1",
            run_context="interactive",
        )
    )
    assert '"success": true' in made, made
    code, out, err = cli(
        scope, "create", "every 30m", "ping", "--model", "claude-x", "--provider", "anthropic"
    )
    assert code == 0, (out, err)
    jobs = demo.host().list_jobs()
    build = only(jobs, lambda j: j.prompt == "check the build")
    inbox = only(jobs, lambda j: j.prompt == "summarise the inbox")
    nokey = only(jobs, lambda j: j.provider == "anthropic")
    assert [build.id, inbox.id, nokey.id] == list(DISPATCH_ORDERS[order])
    assert build.recurring and build.deliver == "local" and build.repeat.times is None
    assert build.next_run_at == start + timedelta(minutes=30)
    assert inbox.deliver == "local" and inbox.next_run_at == start + timedelta(hours=1)
    demo.serve(
        build.id,
        "Build #812 is green.",
        "[SILENT]",
        "[JOB_FAILED]\nthe build is red",
        "[JOB_FAILED]\nthe build is red",
        "[JOB_FAILED]\nthe build is red",
        "Build is green again.",
        "caught up after the pause",
        "caught up after the downtime",
    )
    demo.serve(inbox.id, "3 new mails, nothing urgent.", "2 new mails.", "1 new mail.")

    # -- first slot: the build check runs and is delivered; the missing key blocks --------------
    demo.advance_to(start + timedelta(minutes=30))
    report = demo.tick()
    assert report.status == "ran" and report.outcomes[build.id].status == "ok"
    doc = report.outcomes[build.id].document
    assert doc is not None and doc.parent == scope.home / "cron" / "output" / build.id
    stored = read_document(doc)
    assert stored is not None and stored.section("response") == "Build #812 is green."
    (first,) = demo.attempts(build.id)
    assert first.state == "completed" and first.delivery_status == "delivered"
    (sent,) = demo.sent_for(build.id)
    assert "Build #812 is green." in sent.text and not sent.for_failure
    after = demo.job(build.id)
    assert after.last_status == "ok" and after.repeat.completed == 1
    assert after.last_run_at is not None and after.next_run_at is not None
    assert after.next_run_at - after.last_run_at == timedelta(minutes=30)
    blocked = report.outcomes[nokey.id]
    assert blocked.status == "blocked_config" and blocked.deliver
    assert "no usable credential" in (blocked.error or "")
    assert demo.calls(nokey.id) == 0  # no model call, no tokens
    (alert,) = demo.sent_for(nokey.id)  # one alert for the block
    assert alert.for_failure and "no usable credential" in alert.text

    # -- the hour: silence is not delivered; the hourly inbox job fires; no second block alert --
    # An interval steps from the end of its own last run (drift kept, P14-02) and the pool is
    # serial, so the two 30-minute jobs are next due a few seconds apart: tick once both are.
    second = demo.job(build.id).next_run_at
    again = demo.job(nokey.id).next_run_at
    assert second is not None and second >= start + timedelta(hours=1)
    assert again is not None and abs(again - second) < timedelta(minutes=1)
    demo.advance_to(max(second, again))
    report = demo.tick()
    assert report.outcomes[build.id].status == "silent"
    assert demo.attempts(build.id)[-1].delivery_status is None
    assert len(demo.sent_for(build.id)) == 1  # nothing new went out
    assert documents(scope.home, build.id)[-1].exists()  # the silent run is still saved
    assert nokey.id in report.outcomes and not report.outcomes[nokey.id].deliver
    assert len(demo.sent_for(nokey.id)) == 1  # blocked again, alerted once
    assert report.outcomes[inbox.id].status == "ok"  # the agent-made job, same tick
    assert len(demo.sent_for(inbox.id)) == 1
    assert demo.calls(nokey.id) == 0

    # -- three failures in a row: the first is delivered, the streak counts, one incident holds
    # the repeats (and with them the third run's review nudge) until its reminder window --------
    for n in range(1, 4):
        demo.advance_to(demo.job(build.id).next_run_at or start)
        report = demo.tick()
        failed = report.outcomes[build.id]
        assert failed.status == "failed" and failed.error == "the build is red"
        assert demo.job(build.id).failure_streak == n
    failures = [r for r in demo.sent_for(build.id) if r.for_failure]
    assert len(failures) == 1  # the incident alerted once; the repeats were held
    book = IncidentLedger.for_scope(scope)
    (incident,) = book.list(job_id=build.id)
    assert incident.state == "alerted"
    assert demo.job(build.id).last_status == "failed"

    # -- success: streak reset, incident resolved ----------------------------------------------
    demo.advance_to(demo.job(build.id).next_run_at or start)
    report = demo.tick()
    assert report.outcomes[build.id].status == "ok"
    assert demo.job(build.id).failure_streak == 0
    assert book.list(job_id=build.id)[0].state == "resolved"

    # -- pause, two hours pass, resume: one catch-up run, never one per missed slot -------------
    assert cli(scope, "pause", build.id, "--reason", "holiday")[0] == 0
    demo.advance_to(clock.now() + timedelta(hours=2))
    report = demo.tick()
    assert build.id not in report.outcomes
    assert cli(scope, "resume", build.id)[0] == 0
    report = demo.tick()
    assert report.outcomes[build.id].status == "ok"
    resumed = demo.job(build.id)
    assert resumed.last_dispatch is not None and resumed.last_dispatch.lateness == "catch_up"
    assert resumed.next_run_at is not None and resumed.next_run_at > clock.now()
    assert demo.tick().outcomes.get(build.id) is None  # nothing more is due

    # -- downtime (no ticker for 3 h): one catch-up run ------------------------------------------
    demo.advance_to(clock.now() + timedelta(hours=3))
    before = len(demo.attempts(build.id))
    report = demo.tick()
    assert report.outcomes[build.id].status == "ok"
    assert len(demo.attempts(build.id)) == before + 1

    # -- a process dies mid-run: the attempt is unknown after reaping and never run again --------
    slot = demo.job(build.id).next_run_at
    assert slot is not None
    demo.advance_to(slot)
    with _closing(CronContext.open(scope, procs=demo.pids.as_process(DEAD))) as dying:
        ledger = ExecutionLedger(dying.db, procs=demo.pids.as_process(DEAD))
        attempt = ledger.claim(build.id, scheduled_for=slot)
        ledger.start(attempt.attempt_id)
    demo.pids.kill(DEAD)
    calls = demo.calls(build.id)
    report = demo.tick()
    assert build.id not in report.outcomes and demo.calls(build.id) == calls
    lost = next(a for a in demo.attempts(build.id) if a.attempt_id == attempt.attempt_id)
    assert lost.state == "unknown"
    moved = demo.job(build.id).next_run_at
    assert moved is not None and moved > slot  # that slot is never run again

    # -- `indus cron status` / `list` / `doctor` read the same state -----------------------------
    code, out, _ = cli(scope, "status")
    assert code == 0 and "ticker: alive" in out and "store: writable" in out
    code, out, _ = cli(scope, "list")
    assert build.id in out and inbox.id in out


@contextlib.contextmanager
def _closing(ctx: CronContext) -> Iterator[CronContext]:
    try:
        yield ctx
    finally:
        ctx.close()


def test_a_late_run_in_the_repeated_fall_back_hour_fires_once(
    demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daily 01:30 in New York on the fall-back night (2026-11-01): the first 01:30 (EDT) runs
    late, inside the repeated hour; bookkeeping plans from the slot, so the second 01:30 (EST)
    does not fire again — the next run is the following night."""
    zoned = demo.scope.with_config(
        DictConfig(
            {
                "model": {"provider": "openai", "default": "gpt-5"},
                "timezone": "America/New_York",
                "cron": {"max_parallel_jobs": 1},
            }
        )
    )
    demo.scope = zoned
    first_fold = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
    second_fold = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 EST
    demo.advance_to(first_fold - timedelta(hours=2))
    job = demo.host().create({"schedule": "30 1 * * *", "prompt": "nightly report"})
    assert job.next_run_at == first_fold
    demo.serve(job.id, "night one", "should not run")
    demo.advance_to(first_fold + timedelta(minutes=40))  # 02:10 EDT == 01:10 EST: late
    report = demo.tick()
    assert report.outcomes[job.id].status == "ok"
    planned = demo.job(job.id).next_run_at
    assert planned == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)  # tomorrow 01:30 EST
    demo.advance_to(second_fold + timedelta(minutes=1))
    assert job.id not in demo.tick().outcomes
    assert demo.calls(job.id) == 1
