"""Output documents, retention and bounded cleanup (P14-12)."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from indus.cron.output import (
    bounded,
    bounded_async,
    documents,
    format_failure,
    prune,
    read_document,
    remove_job_output,
    write_document,
)
from indus.foundation.scope import Scope
from tests.fakes.agent import run_driven
from tests.fakes.clock import FakeClock

JOB = "a1b2c3d4e5f6"
WHEN = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def _write(home: Path, when: datetime = WHEN, **sections: str) -> Path:
    return write_document(
        home,
        job_id=JOB,
        label="build check",
        schedule="every 30m",
        status="ok",
        when=when,
        sections=sections or {"prompt": "p", "response": "r"},
    )


def test_document_path_and_round_trip(tmp_path: Path) -> None:
    tricky = "line one\n--- response: 3 chars ---\nabc\nend"
    path = _write(tmp_path, prompt="the prompt\nwith two lines", response=tricky)
    assert path == tmp_path / "cron" / "output" / JOB / "20260101T090000Z.md"
    doc = read_document(path)
    assert doc is not None
    assert doc.status == "ok"
    assert doc.fields["job"] == JOB and doc.fields["schedule"] == "every 30m"
    assert doc.section("prompt") == "the prompt\nwith two lines"
    assert doc.section("response") == tricky  # a quoted frame line is never a boundary


def test_same_second_never_overwrites(tmp_path: Path) -> None:
    first = _write(tmp_path)
    second = _write(tmp_path)
    assert first != second and first.exists() and second.exists()
    assert documents(tmp_path, JOB)[0] == second  # newest first


def test_truncated_section_is_not_trusted(tmp_path: Path) -> None:
    path = _write(tmp_path, prompt="p", response="the whole answer")
    path.write_text(path.read_text(encoding="utf-8")[:-6], encoding="utf-8")
    doc = read_document(path)
    assert doc is not None and "response" not in doc.sections


def test_fifty_first_document_prunes_the_oldest(tmp_path: Path) -> None:
    paths = [_write(tmp_path, WHEN + timedelta(minutes=i)) for i in range(51)]
    assert prune(tmp_path, JOB, 50) == 1
    left = documents(tmp_path, JOB)
    assert len(left) == 50 and paths[0] not in left and paths[-1] in left


def test_retention_zero_keeps_everything(tmp_path: Path) -> None:
    for i in range(3):
        _write(tmp_path, WHEN + timedelta(minutes=i))
    assert prune(tmp_path, JOB, 0) == 0 and prune(tmp_path, JOB, -1) == 0
    assert len(documents(tmp_path, JOB)) == 3


def test_remove_job_output(tmp_path: Path) -> None:
    _write(tmp_path)
    remove_job_output(tmp_path, JOB)
    assert documents(tmp_path, JOB) == []


def test_traceback_is_chained_and_redacted(scope: Scope) -> None:
    secret = "sk" + "-proj-" + "Zq8Lm3Np5Rt7Vx9Bz2Cd4Fg6Hj8Kl0Mn"
    try:
        try:
            raise OSError(f"connect failed with key {secret}")
        except OSError as inner:
            raise RuntimeError("the agent run failed") from inner
    except RuntimeError as exc:
        text = format_failure(exc, scope=scope)
    assert "OSError" in text and "RuntimeError" in text
    assert "direct cause" in text
    assert secret not in text


def test_hung_cleanup_is_abandoned_at_ten_seconds(fake_clock: FakeClock) -> None:
    release = threading.Event()

    async def main() -> tuple[bool, float]:
        start = fake_clock.monotonic()
        ok = await bounded(
            lambda: release.wait(30), seconds=10.0, clock=fake_clock, label="stuck finaliser"
        )
        return ok, fake_clock.monotonic() - start

    try:
        ok, waited = asyncio.run(run_driven(main(), fake_clock, step=0.5))
    finally:
        release.set()
    assert ok is False
    assert 10.0 <= waited < 11.0


def test_quick_cleanup_finishes(fake_clock: FakeClock) -> None:
    done: list[int] = []

    async def main() -> bool:
        return await bounded(lambda: done.append(1), seconds=10.0, clock=fake_clock, label="ok")

    assert asyncio.run(run_driven(main(), fake_clock, step=0.01)) is True
    assert done == [1]


def test_hung_async_cleanup_is_cancelled(fake_clock: FakeClock) -> None:
    async def never() -> None:
        await asyncio.Event().wait()

    async def main() -> bool:
        return await bounded_async(never(), seconds=10.0, clock=fake_clock, label="close")

    assert asyncio.run(run_driven(main(), fake_clock, step=1.0)) is False
