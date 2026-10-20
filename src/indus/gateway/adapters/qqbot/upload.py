"""QQ Bot chunked media upload (P16-20).

A local file reaches a C2C or group chat in three steps:

1. ``upload_prepare`` with the file's type, name, size, MD5, SHA-1 and the MD5 of its first
   10,002,432 bytes → an upload id, the block size, one pre-signed URL per part, how many parts may
   go at once and how long a part may keep answering "not yet";
2. per part: PUT the bytes to its pre-signed URL (300 s each, 2 retries = 3 tries, waiting 1 s then
   2 s), then ``upload_part_finish`` with the part's index, size and MD5 — retried every second
   while QQ answers the "retry" business code 40093001, until the prepare answer's
   ``retry_timeout`` (default 120 s, never more than 600 s);
3. ``files`` with only the upload id → the ``file_info`` a media message carries (2 retries,
   waiting 2 s then 4 s).

Parts go one at a time unless QQ allows more (at most 10). The daily upload quota (business code
40093002 on prepare) is final: `DailyLimitReached`, never retried. Every wait uses the injected
clock; file reads and hashing run off the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.gateway.adapters.qqbot.client import QQApiError

__all__ = [
    "COMPLETE_RETRIES",
    "DAILY_LIMIT_CODE",
    "MAX_CONCURRENCY",
    "MD5_HEAD_BYTES",
    "PART_FINISH_RETRY_CODE",
    "PART_RETRIES",
    "ChunkedUploader",
    "DailyLimitReached",
    "FileTooLarge",
    "Part",
    "Prepared",
    "UploadFailed",
    "file_hashes",
    "parse_prepare",
]

log = logging.getLogger(__name__)

DAILY_LIMIT_CODE = 40093002
PART_FINISH_RETRY_CODE = 40093001
MAX_CONCURRENCY = 10
PART_RETRIES = 2
PART_RETRY_BASE_S = 1.0
FINISH_INTERVAL_S = 1.0
FINISH_DEFAULT_S = 120.0
FINISH_MAX_S = 600.0
COMPLETE_RETRIES = 2
COMPLETE_RETRY_BASE_S = 2.0
MD5_HEAD_BYTES = 10_002_432
_READ_BLOCK = 1024 * 1024

# ``api(endpoint, body)`` posts to ``/v2/{users|groups}/{id}/{endpoint}``; ``put(url, data)``
# returns the HTTP status of the PUT to a pre-signed URL.
type ApiCall = Callable[[str, Mapping[str, Any]], Awaitable[dict[str, Any]]]
type PutPart = Callable[[str, bytes], Awaitable[int]]


class UploadFailed(Exception):
    """The upload could not finish (the message says which step)."""


class DailyLimitReached(UploadFailed):
    """QQ's daily upload quota is used up: retrying today cannot help."""


class FileTooLarge(UploadFailed):
    pass


@dataclass(frozen=True)
class Part:
    index: int
    url: str
    size: int = 0


@dataclass(frozen=True)
class Prepared:
    upload_id: str
    block_size: int
    parts: tuple[Part, ...]
    concurrency: int = 1
    retry_timeout: float = 0.0


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return default


def parse_prepare(raw: Mapping[str, Any]) -> Prepared:
    """The prepare answer (bare or inside ``data``) → `Prepared` (`UploadFailed` when unusable)."""
    inner = raw.get("data")
    src = cast(dict[str, Any], inner) if isinstance(inner, dict) else dict(raw)
    upload_id = str(src.get("upload_id") or "")
    listed = src.get("parts") or src.get("part_list")
    if not upload_id or not isinstance(listed, list) or not listed:
        raise UploadFailed("QQ's upload_prepare answer has no upload id or parts")
    parts: list[Part] = []
    for item in cast(list[object], listed):
        if not isinstance(item, dict):
            continue
        fields = cast(dict[str, Any], item)
        index = _int(fields.get("part_index") or fields.get("index"))
        url = str(fields.get("presigned_url") or fields.get("url") or "")
        if index < 1 or not url:
            raise UploadFailed("QQ's upload_prepare answer has a part without index or URL")
        parts.append(Part(index, url, _int(fields.get("block_size"))))
    try:
        timeout = float(src.get("retry_timeout") or 0.0)
    except (TypeError, ValueError):
        timeout = 0.0
    return Prepared(
        upload_id=upload_id,
        block_size=_int(src.get("block_size")),
        parts=tuple(sorted(parts, key=lambda p: p.index)),
        concurrency=max(1, _int(src.get("concurrency"), 1)),
        retry_timeout=max(timeout, 0.0),
    )


def _hash_file(path: str, size: int) -> dict[str, str]:
    md5, sha1, head = hashlib.md5(), hashlib.sha1(), hashlib.md5()
    seen = 0
    with open(path, "rb") as fh:
        while block := fh.read(_READ_BLOCK):
            md5.update(block)
            sha1.update(block)
            if seen < MD5_HEAD_BYTES:
                head.update(block[: MD5_HEAD_BYTES - seen])
            seen += len(block)
    whole = md5.hexdigest()
    return {
        "md5": whole,
        "sha1": sha1.hexdigest(),
        "md5_10m": head.hexdigest() if size > MD5_HEAD_BYTES else whole,
    }


async def file_hashes(path: str, size: int) -> dict[str, str]:
    """MD5, SHA-1 and the MD5 of the first 10,002,432 bytes, off the event loop."""
    return await run_blocking(_hash_file, path, size, label="qqbot upload hash")


def _read_part(path: str, offset: int, length: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
    if len(data) != length:
        raise UploadFailed(
            f"the file changed while uploading (short read at byte {offset}: {len(data)} of "
            f"{length})"
        )
    return data


class ChunkedUploader:
    """One upload at a time per instance (see the module notes)."""

    def __init__(self, *, api: ApiCall, put: PutPart, clock: Clock) -> None:
        self._api = api
        self._put = put
        self.clock = clock
        self.puts: list[int] = []
        self.finishes: list[int] = []

    async def upload(
        self, path: str, *, file_type: int, file_name: str, max_bytes: int | None = None
    ) -> dict[str, Any]:
        size = (await run_blocking(Path(path).stat, label="qqbot upload")).st_size
        if max_bytes is not None and size > max_bytes:
            raise FileTooLarge(f"{file_name} is {size} bytes; the limit is {max_bytes}")
        hashes = await file_hashes(path, size)
        body = {"file_type": file_type, "file_name": file_name, "file_size": size, **hashes}
        try:
            prepared = parse_prepare(await self._api("upload_prepare", body))
        except QQApiError as exc:
            if exc.code == DAILY_LIMIT_CODE:
                raise DailyLimitReached(
                    f"QQ's daily upload limit is reached ({file_name}, {size} bytes); try again "
                    "tomorrow"
                ) from None
            raise
        timeout = prepared.retry_timeout or FINISH_DEFAULT_S
        finish_timeout = min(timeout, FINISH_MAX_S)
        gate = asyncio.Semaphore(min(prepared.concurrency, MAX_CONCURRENCY))

        async def one(part: Part) -> None:
            async with gate:
                await self._part(path, size, prepared, part, finish_timeout)

        tasks = [asyncio.ensure_future(one(part)) for part in prepared.parts]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        return await self._complete(prepared.upload_id)

    async def _part(
        self, path: str, size: int, prepared: Prepared, part: Part, finish_timeout: float
    ) -> None:
        block = part.size or prepared.block_size
        offset = (part.index - 1) * prepared.block_size
        length = min(block, size - offset)
        if block <= 0 or length <= 0:
            raise UploadFailed(f"part {part.index} lies outside the file")
        data = await run_blocking(_read_part, path, offset, length, label="qqbot upload")
        md5 = hashlib.md5(data).hexdigest()
        await self._put_with_retries(part, data)
        await self._finish(prepared.upload_id, part.index, length, md5, finish_timeout)

    async def _put_with_retries(self, part: Part, data: bytes) -> None:
        last = ""
        for attempt in range(PART_RETRIES + 1):
            self.puts.append(part.index)
            try:
                status = await self._put(part.url, data)
            except Exception as exc:
                last = type(exc).__name__
            else:
                if 200 <= status < 300:
                    return
                last = f"HTTP {status}"
            if attempt < PART_RETRIES:
                await self.clock.sleep(PART_RETRY_BASE_S * 2**attempt)
        raise UploadFailed(f"part {part.index} failed after {PART_RETRIES + 1} tries ({last})")

    async def _finish(
        self, upload_id: str, index: int, length: int, md5: str, timeout: float
    ) -> None:
        body = {"upload_id": upload_id, "part_index": index, "block_size": length, "md5": md5}
        started = self.clock.monotonic()
        while True:
            self.finishes.append(index)
            try:
                await self._api("upload_part_finish", body)
                return
            except QQApiError as exc:
                if exc.code != PART_FINISH_RETRY_CODE:
                    raise
            if self.clock.monotonic() - started >= timeout:
                raise UploadFailed(f"part {index} was not accepted within {timeout:.0f} s")
            await self.clock.sleep(FINISH_INTERVAL_S)

    async def _complete(self, upload_id: str) -> dict[str, Any]:
        last: BaseException | None = None
        for attempt in range(COMPLETE_RETRIES + 1):
            try:
                return await self._api("files", {"upload_id": upload_id})
            except QQApiError as exc:
                last = exc
                if exc.status < 500 and exc.status != 429:
                    raise
            except Exception as exc:
                last = exc
            if attempt < COMPLETE_RETRIES:
                await self.clock.sleep(COMPLETE_RETRY_BASE_S * 2**attempt)
        raise UploadFailed(
            f"completing the upload failed after {COMPLETE_RETRIES + 1} tries "
            f"({type(last).__name__})"
        )
