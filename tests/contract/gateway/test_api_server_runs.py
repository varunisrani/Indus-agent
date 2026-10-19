"""P15-31: runs (attended turns over HTTP), sessions and cron jobs of the API server."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from indus.cron.ports import RunReport
from indus.foundation.scope import Scope
from indus.gateway.http.api_server import ApiServer
from indus.gateway.http.runs import BACKLOG_LIMIT, STATUS_TTL_S, STREAM_TTL_S
from indus.service.cron_host import CronHost
from tests.contract.gateway._http_kit import (
    DANGEROUS,
    ApiKit,
    breathe,
    build_api,
    sse_events,
    until,
    within,
)
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn, tool_batch


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def stalled(text: str = "working", seconds: float = 1000.0) -> Turn:
    return Turn((Text((text,)), Stall(seconds), Finish("stop")))


async def start(kit: ApiKit, text: str = "go", **body: Any) -> str:
    async with kit.client() as client:
        answer = await within(client.post("/v1/runs", json={"input": text, **body}))
    assert answer.status_code == 202, answer.text
    data = answer.json()
    assert data["status"] == "started" and data["run_id"].startswith("run_")
    return str(data["run_id"])


async def status(kit: ApiKit, run_id: str) -> dict[str, Any]:
    async with kit.client() as client:
        answer = await client.get(f"/v1/runs/{run_id}")
    assert answer.status_code == 200, answer.text
    return answer.json()


async def settled(kit: ApiKit, run_id: str) -> dict[str, Any]:
    found: dict[str, Any] = {}

    async def look() -> bool:
        found.update(await status(kit, run_id))
        return found["status"] in {"completed", "failed", "cancelled", "unknown"}

    for _ in range(2000):
        if await look():
            return found
        await breathe()
    raise AssertionError(f"run {run_id} never ended: {found}")


def names(frames: list[dict[str, Any]]) -> list[str]:
    return [f["data"]["event"] for f in frames if isinstance(f.get("data"), dict)]


# -- approvals ----------------------------------------------------------------------------------


def test_a_run_waits_for_approval_and_the_approval_route_settles_it(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_api(
            scope, fake_clock, tmp_path, tool_batch(("flagged", {})), text_turn("pushed")
        )
        try:
            run_id = await start(kit, "push it")
            async with kit.client() as client:
                events = asyncio.ensure_future(client.get(f"/v1/runs/{run_id}/events"))
                await until(lambda: kit.ctx.runs.runs[run_id].status == "waiting_for_approval")
                waiting = await status(kit, run_id)
                approval = waiting["approval"]
                assert DANGEROUS.split()[0] in approval["command"]
                assert "once" in approval["choices"] and "deny" in approval["choices"]
                url = f"/v1/runs/{run_id}/approval"
                bare = await client.post(url, json={"choice": "yes"})
                assert bare.status_code == 400  # a bare "yes" never approves
                wrong = await client.post(url, json={"choice": "once", "request_id": "r_other"})
                assert wrong.status_code == 409
                ok = await client.post(url, json={"choice": "once"})
                assert ok.status_code == 200 and ok.json()["choice"] == "once"
                again = await client.post(url, json={"choice": "once"})
                assert again.status_code == 409
                stream = await within(events)
            frames = sse_events(stream.text)
            assert frames[0] == {"comment": "open"}
            seen = names(frames)
            assert seen[0] == "run.started"
            assert seen.index("approval.request") < seen.index("approval.responded")
            assert seen[-1] == "run.completed"
            data = [f for f in frames if isinstance(f.get("data"), dict)]
            assert [f["id"] for f in data] == [f["data"]["seq"] for f in data]
            final = await settled(kit, run_id)
            assert final["status"] == "completed" and final["output"] == "pushed"
            tool_rows = [m for m in kit.model_messages(-1) if m.get("role") == "tool"]
            assert "ran (user_approved)" in str(tool_rows[0]["content"])
        finally:
            await kit.close()

    run(main())


def test_a_denied_approval_reaches_the_model_as_denied(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_api(
            scope, fake_clock, tmp_path, tool_batch(("flagged", {})), text_turn("ok, not pushed")
        )
        try:
            run_id = await start(kit, "push it")
            await until(lambda: kit.ctx.runs.runs[run_id].status == "waiting_for_approval")
            async with kit.client() as client:
                answer = await client.post(
                    f"/v1/runs/{run_id}/approval", json={"choice": "deny", "reason": "not today"}
                )
            assert answer.status_code == 200
            assert (await settled(kit, run_id))["status"] == "completed"
            tool_rows = [m for m in kit.model_messages(-1) if m.get("role") == "tool"]
            assert "blocked (denied)" in str(tool_rows[0]["content"])
            assert "not today" in str(tool_rows[0]["content"])
        finally:
            await kit.close()

    run(main())


# -- limits and lifetimes -----------------------------------------------------------------------


def test_the_eleventh_concurrent_run_gets_429(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_api(
            scope,
            fake_clock,
            tmp_path,
            *[stalled(f"r{i}", 100.0) for i in range(10)],
            text_turn("late"),
        )
        try:
            ids = [await start(kit, f"job {i}") for i in range(10)]
            await until(lambda: len(kit.server.model_requests) == 10)
            async with kit.client() as client:
                refused = await client.post("/v1/runs", json={"input": "one more"})
            assert refused.status_code == 429
            assert refused.json()["error"]["code"] == "too_many_runs"
            fake_clock.advance(100.0)
            for run_id in ids:
                assert (await settled(kit, run_id))["status"] == "completed"
            late = await start(kit, "now there is room")
            assert (await settled(kit, late))["output"] == "late"
        finally:
            await kit.close()

    run(main())


def test_terminal_status_is_kept_an_hour_and_events_five_minutes(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, text_turn("done"))
        try:
            run_id = await start(kit)
            await settled(kit, run_id)
            async with kit.client() as client:
                replay = await client.get(f"/v1/runs/{run_id}/events")
                assert names(sse_events(replay.text))[-1] == "run.completed"
                fake_clock.advance(STREAM_TTL_S + 1)
                gone = await client.get(f"/v1/runs/{run_id}/events")
                assert gone.status_code == 410
                fake_clock.advance(STATUS_TTL_S - STREAM_TTL_S - 2)  # 3,599 s after the end
                assert (await client.get(f"/v1/runs/{run_id}")).status_code == 200
                fake_clock.advance(2.0)
                assert (await client.get(f"/v1/runs/{run_id}")).status_code == 404
        finally:
            await kit.close()

    run(main())


def test_last_event_id_replays_only_later_events(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, text_turn("a", "b", "c"))
        try:
            run_id = await start(kit)
            await settled(kit, run_id)
            async with kit.client() as client:
                full = sse_events((await client.get(f"/v1/runs/{run_id}/events")).text)
                later = sse_events(
                    (
                        await client.get(
                            f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
                        )
                    ).text
                )
            all_ids = [f["id"] for f in full if "id" in f]
            assert [f["id"] for f in later if "id" in f] == [i for i in all_ids if i > 1]
            deltas = [
                f["data"]["delta"]
                for f in full
                if f.get("data", {}).get("event") == "message.delta"
            ]
            assert "".join(deltas) == "abc"
            assert BACKLOG_LIMIT == 1_000
        finally:
            await kit.close()

    run(main())


# -- controls -----------------------------------------------------------------------------------


def test_steer_and_stop(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, stalled(), text_turn("after"))
        try:
            run_id = await start(kit)
            await until(lambda: kit.server.model_requests)
            async with kit.client() as client:
                empty = await client.post(f"/v1/runs/{run_id}/steer", json={"input": " "})
                assert empty.status_code == 400
                steer = await client.post(f"/v1/runs/{run_id}/steer", json={"input": "also X"})
                assert steer.status_code == 200 and steer.json()["accepted"] is True
                stop = await client.post(f"/v1/runs/{run_id}/stop")
                assert stop.json()["status"] == "stopping"
                final = await settled(kit, run_id)
                assert final["status"] == "cancelled"
                late = await client.post(f"/v1/runs/{run_id}/steer", json={"input": "more"})
                assert late.status_code == 409
                again = await client.post(f"/v1/runs/{run_id}/stop")
                assert again.json()["status"] == "cancelled"
            assert kit.ctx.gate.active == 0
        finally:
            await kit.close()

    run(main())


def test_resolve_unknown_needs_the_runs_generation(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, text_turn("fine"), text_turn("lost"))
        try:
            done = await start(kit)
            await settled(kit, done)
            async with kit.client() as client:
                refused = await client.post(
                    f"/v1/runs/{done}/resolve-unknown", json={"generation": 1}
                )
                assert refused.status_code == 409

                real_wait = kit.service.wait

                async def broken_wait(session_id: str, turn_id: str | None = None) -> Any:
                    await real_wait(session_id, turn_id)
                    raise RuntimeError("the result was lost")

                monkeypatch.setattr(kit.service, "wait", broken_wait)
                lost = await start(kit)
                final = await settled(kit, lost)
                assert final["status"] == "unknown"
                generation = final["generation"]
                url = f"/v1/runs/{lost}/resolve-unknown"
                wrong = await client.post(url, json={"generation": generation + 1})
                assert wrong.status_code == 409
                ok = await client.post(url, json={"generation": generation})
                assert ok.status_code == 200 and ok.json()["status"] == "resolved"
        finally:
            await kit.close()

    run(main())


def test_a_run_belongs_to_the_key_that_started_it(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, text_turn("mine"))
        try:
            run_id = await start(kit)
            _, other = kit.ctx.keys.create("someone else")
            async with kit.client(key=other) as client:
                assert (await client.get(f"/v1/runs/{run_id}")).status_code == 404
                assert (await client.post(f"/v1/runs/{run_id}/stop")).status_code == 404
            await settled(kit, run_id)
        finally:
            await kit.close()

    run(main())


# -- sessions -----------------------------------------------------------------------------------


def test_sessions_list_get_and_background_completions(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, text_turn("hello"), text_turn("later"))
        try:
            async with kit.client() as client:
                chat = {"messages": [{"role": "user", "content": "hi"}]}
                answer = await within(client.post("/v1/chat/completions", json=chat))
                sid = answer.headers["X-Indus-Session-Id"]
                listed = (await client.get("/api/sessions?limit=5")).json()
                assert sid in [row["id"] for row in listed["data"]]
                # Background work finishing later starts its own turn: it is recorded for pollers.
                assert await kit.service.inject(sid, "subagent finished", origin="test")
                await until(lambda: bool(kit.ctx.completions and kit.ctx.completions.recent(sid)))
                one = (await client.get(f"/api/sessions/{sid}")).json()
                assert one["id"] == sid and "system_prompt" not in one
                assert one["completions"][0]["text"] == "later"
                assert (await client.get("/api/sessions/s_missing")).status_code == 404
                assert (await client.get("/api/sessions?limit=x")).status_code == 400
        finally:
            await kit.close()

    run(main())


# -- jobs ---------------------------------------------------------------------------------------


class RecordingHost(CronHost):
    """The real P14 host; runs are recorded instead of started."""

    def __init__(self, scope: Scope) -> None:
        super().__init__(scope)
        self.ran: list[str] = []

    async def run_now(self, job_id: str, *, context: str | None = None) -> RunReport:
        self.ran.append(job_id)
        return RunReport(job_id, "ok")


def test_jobs_crud_runs_and_limits(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    host = RecordingHost(scope)

    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, cron=lambda: host)
        try:
            async with kit.client() as client:
                long_name = await client.post(
                    "/api/jobs", json={"name": "n" * 201, "schedule": "every 30m", "prompt": "p"}
                )
                assert long_name.status_code == 422
                assert long_name.json()["error"]["param"] == "name"
                long_prompt = await client.post(
                    "/api/jobs", json={"name": "n", "schedule": "every 30m", "prompt": "p" * 5001}
                )
                assert long_prompt.status_code == 422
                script = await client.post(
                    "/api/jobs", json={"name": "n", "schedule": "every 30m", "script": "x.py"}
                )
                assert script.status_code == 422
                bad_schedule = await client.post(
                    "/api/jobs", json={"name": "n", "schedule": "whenever", "prompt": "p"}
                )
                assert bad_schedule.status_code == 400
                made = await client.post(
                    "/api/jobs",
                    json={"name": "n" * 200, "schedule": "every 30m", "prompt": "check the build"},
                )
                assert made.status_code == 201, made.text
                job = made.json()["job"]
                job_id = job["id"]
                assert len(job_id) == 12 and job["prompt"] == "check the build"
                listed = (await client.get("/api/jobs")).json()["data"]
                assert [j["id"] for j in listed] == [job_id]
                assert (await client.get(f"/api/jobs/{job_id}")).json()["job"]["id"] == job_id
                assert (await client.get("/api/jobs/NOT-HEX")).status_code == 400
                assert (await client.get("/api/jobs/0123456789ab")).status_code == 404
                renamed = await client.patch(f"/api/jobs/{job_id}", json={"name": "build check"})
                assert renamed.json()["job"]["name"] == "build check"
                assert (await client.patch(f"/api/jobs/{job_id}", json={})).status_code == 400
                paused = (await client.post(f"/api/jobs/{job_id}/pause")).json()["job"]
                assert paused["state"] == "paused"
                resumed = (await client.post(f"/api/jobs/{job_id}/resume")).json()["job"]
                assert resumed["state"] == "scheduled"
                ran = await client.post(f"/api/jobs/{job_id}/run")
                assert ran.status_code == 202
                fired = await client.post("/api/cron/fire", json={"job_id": job_id})
                assert fired.status_code == 202
                assert (
                    await client.post("/api/cron/fire", json={"job_id": "x"})
                ).status_code == 400
                await until(lambda: host.ran == [job_id, job_id])
                assert (await client.delete(f"/api/jobs/{job_id}")).json()["deleted"] is True
                assert (await client.delete(f"/api/jobs/{job_id}")).status_code == 404
        finally:
            await kit.close()

    run(main())


class HangingHost(CronHost):
    """A run that never finishes on its own (until the API server closes)."""

    def __init__(self, scope: Scope) -> None:
        super().__init__(scope)
        self.started = asyncio.Event()
        self.cancelled = False

    async def run_now(self, job_id: str, *, context: str | None = None) -> RunReport:
        self.started.set()
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return RunReport(job_id, "ok")


def test_api_started_job_runs_are_cancelled_when_the_server_closes(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    """Integration: ``/api/jobs/{id}/run`` tasks belong to the API server; its close (the
    daemon's stop of the HTTP part) cancels and awaits them."""
    host = HangingHost(scope)

    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, cron=lambda: host)
        try:
            async with kit.client() as client:
                made = await client.post(
                    "/api/jobs", json={"name": "n", "schedule": "every 30m", "prompt": "p"}
                )
                job_id = made.json()["job"]["id"]
                assert (await client.post(f"/api/jobs/{job_id}/run")).status_code == 202
            await asyncio.wait_for(host.started.wait(), 10)
            assert len(kit.ctx.jobs) == 1
            await ApiServer(kit.ctx).aclose()
            assert host.cancelled and not kit.ctx.jobs
        finally:
            await kit.service.aclose()
            kit.store.close()

    run(main())
