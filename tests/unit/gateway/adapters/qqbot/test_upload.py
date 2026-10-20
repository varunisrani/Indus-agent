"""QQ Bot chunked media upload (P16-20) over the fake QQ API."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.gateway.adapters.qqbot import upload as upload_mod
from indus.gateway.adapters.qqbot.client import QQApiError, QQClient, QQTokens
from indus.gateway.adapters.qqbot.upload import (
    MAX_CONCURRENCY,
    PART_RETRIES,
    ChunkedUploader,
    DailyLimitReached,
    FileTooLarge,
    UploadFailed,
    file_hashes,
    parse_prepare,
)
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.qqbot_wire import QQWire


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


async def drive[T](clock: FakeClock, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(4000):
        await clock.run_until_idle()
        # File reads and hashing run on a worker thread: give it real time before moving on.
        await asyncio.wait({task}, timeout=0.002)
        if task.done():
            return task.result()
        clock.advance(step)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


class Rig:
    def __init__(self, tmp_path: Path, data: bytes = b"0123456789") -> None:
        self.clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
        self.wire = QQWire(FakeChatPlatform("qqbot", clock=self.clock))
        self.path = tmp_path / "report.pdf"
        self.path.write_bytes(data)
        self.data = data

    async def upload(
        self, *, kind: str = "c2c", **kw: Any
    ) -> tuple[dict[str, Any], ChunkedUploader]:
        async with httpx.AsyncClient(transport=self.wire.transport()) as http:
            tokens = QQTokens(self.wire.app_id, self.wire.secret, http, clock=self.clock)
            client = QQClient(http, tokens)

            async def api(endpoint: str, body: Mapping[str, Any]) -> dict[str, Any]:
                return await client.files_call(kind, "U1", endpoint, body)

            uploader = ChunkedUploader(api=api, put=client.put_part, clock=self.clock)
            done = await drive(
                self.clock,
                uploader.upload(str(self.path), file_type=4, file_name=self.path.name, **kw),
            )
            return done, uploader


def test_a_file_goes_up_in_parts_and_comes_back_as_file_info(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    done, uploader = run(rig.upload())
    info = done["file_info"]
    stored = rig.wire.uploads[info]
    assert stored["data"] == rig.data
    assert stored["md5"] == hashlib.md5(rig.data).hexdigest()
    assert stored["sha1"] == hashlib.sha1(rig.data).hexdigest()
    assert stored["md5_10m"] == stored["md5"]
    assert stored["file_size"] == 10 and stored["file_type"] == 4
    assert uploader.puts == [1, 2, 3] and uploader.finishes == [1, 2, 3]
    assert rig.wire.max_parallel_puts == 1  # one part at a time by default


def test_part_retries_are_exact(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.wire.put_failures = {2: PART_RETRIES}
    done, uploader = run(rig.upload())
    assert done["file_info"]
    assert uploader.puts.count(2) == PART_RETRIES + 1 == 3
    assert uploader.puts.count(1) == 1 and uploader.puts.count(3) == 1

    rig = Rig(tmp_path)
    rig.wire.put_failures = {2: PART_RETRIES + 1}
    with pytest.raises(UploadFailed, match="part 2 failed after 3 tries"):
        run(rig.upload())
    upload = next(iter(rig.wire.uploads.values()))
    assert 2 not in upload["finished"]


def test_the_daily_limit_is_never_retried(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.wire.daily_limit = True
    with pytest.raises(DailyLimitReached, match="daily upload limit"):
        run(rig.upload())
    prepares = [c for c in rig.wire.calls if c.path.endswith("/upload_prepare")]
    assert len(prepares) == 1
    assert not any(c.method == "PUT" for c in rig.wire.calls)


def test_part_finish_retries_every_second_until_the_timeout(tmp_path: Path) -> None:
    rig = Rig(tmp_path, b"abcd")
    rig.wire.finish_busy = 5
    done, uploader = run(rig.upload())
    assert done["file_info"]
    assert uploader.finishes == [1] * 6  # five "not yet" answers, then accepted

    rig = Rig(tmp_path, b"abcd")
    rig.wire.finish_busy = 10_000  # the prepare answer allows 30 s
    start = rig.clock.monotonic()
    with pytest.raises(UploadFailed, match="not accepted within 30 s"):
        run(rig.upload())
    assert 30 <= rig.clock.monotonic() - start < 40
    assert 30 <= 10_000 - rig.wire.finish_busy <= 33  # about one try per second


def test_parts_run_in_parallel_only_as_far_as_allowed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, b"x" * 12)
    rig.wire.block_size = 1
    rig.wire.prepare_concurrency = 3
    rig.wire.put_gate = 3  # the first PUTs wait for each other: never hangs only if 3 may overlap
    run(rig.upload())
    assert rig.wire.max_parallel_puts == 3

    rig = Rig(tmp_path, b"x" * 24)
    rig.wire.block_size = 1
    rig.wire.prepare_concurrency = 50
    run(rig.upload())
    assert rig.wire.max_parallel_puts <= MAX_CONCURRENCY


def test_completion_is_retried_twice_for_server_errors_only(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.wire.complete_failures = 2
    done, _ = run(rig.upload())
    assert done["file_info"]
    rig = Rig(tmp_path)
    rig.wire.complete_failures = 3
    with pytest.raises(UploadFailed, match="after 3 tries"):
        run(rig.upload())

    async def bad_request(endpoint: str, body: Mapping[str, Any]) -> dict[str, Any]:
        raise QQApiError(400, "nope", code=40000)

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    uploader = ChunkedUploader(api=bad_request, put=_never_put, clock=clock)
    with pytest.raises(QQApiError):
        run(uploader._complete("u1"))  # pyright: ignore[reportPrivateUsage]


async def _never_put(url: str, data: bytes) -> int:
    raise AssertionError("no PUT expected")


def test_size_limit_and_group_paths(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with pytest.raises(FileTooLarge):
        run(rig.upload(max_bytes=5))
    rig = Rig(tmp_path)
    run(rig.upload(kind="group"))
    assert any(c.path == "/v2/groups/U1/upload_prepare" for c in rig.wire.calls)
    rig = Rig(tmp_path)
    with pytest.raises(ValueError):
        run(rig.upload(kind="channel"))


def test_prepare_answers_are_checked() -> None:
    wrapped = {
        "data": {
            "upload_id": "u",
            "block_size": 5,
            "concurrency": "2",
            "retry_timeout": 9,
            "part_list": [{"index": 2, "url": "https://x/2"}, {"index": 1, "url": "https://x/1"}],
        }
    }
    prepared = parse_prepare(wrapped)
    assert prepared.upload_id == "u" and [p.index for p in prepared.parts] == [1, 2]
    assert prepared.concurrency == 2 and prepared.retry_timeout == 9.0
    for bad in ({}, {"upload_id": "u", "parts": []}, {"upload_id": "u", "parts": [{"index": 1}]}):
        with pytest.raises(UploadFailed):
            parse_prepare(bad)


def test_a_short_read_fails_the_part(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"abc")
    with pytest.raises(UploadFailed, match="short read"):
        upload_mod._read_part(str(path), 2, 4)  # pyright: ignore[reportPrivateUsage]


def test_the_head_hash_covers_only_the_first_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upload_mod, "MD5_HEAD_BYTES", 4)
    path = tmp_path / "f.bin"
    path.write_bytes(b"abcdefgh")
    hashes = run(file_hashes(str(path), 8))
    assert hashes["md5_10m"] == hashlib.md5(b"abcd").hexdigest()
    assert hashes["md5"] == hashlib.md5(b"abcdefgh").hexdigest()
