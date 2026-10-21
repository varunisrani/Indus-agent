"""A fake artifact origin and mirror for pm tests (P24-23): no network, httpx ``MockTransport``.

``FakeOrigin`` serves registered URLs and, under ``mirror`` (``<mirror>/sha256/<hex>``), every
registered blob by its hash. Knobs per URL: serve wrong bytes, ignore ``Range``, cut the
connection after N bytes (once), answer a status, redirect. Every request is recorded with its
headers and the timeout httpx attached.

``tar_gz`` / ``zip_bytes`` build archives in memory; ``definitions_text`` writes a small package
list whose URLs point at the fake origin.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

import httpx

ORIGIN = "https://dl.example"
MIRROR = "https://mirror.example/pm"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tar_gz(files: Mapping[str, bytes], *, top: str = "pkg", mode: int = 0o755) -> bytes:
    """A tar.gz whose members sit under ``top/`` (so ``strip = 1`` drops it)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        folder = tarfile.TarInfo(top)
        folder.type = tarfile.DIRTYPE
        folder.mode = 0o755
        tar.addfile(folder)
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def zip_bytes(
    files: Mapping[str, bytes], *, top: str = "", exec_names: tuple[str, ...] = ()
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(f"{top}/{name}" if top else name)
            mode = 0o100755 if name in exec_names else 0o100644
            info.external_attr = mode << 16
            zf.writestr(info, data)
    return buffer.getvalue()


@dataclass
class Seen:
    url: str
    headers: dict[str, str]
    timeout: object


class _Cut(httpx.SyncByteStream):
    """Yields ``data[:limit]`` and then fails like a dropped connection."""

    def __init__(self, data: bytes, limit: int) -> None:
        self._data = data
        self._limit = limit

    def __iter__(self) -> Iterator[bytes]:
        yield self._data[: self._limit]
        raise httpx.ReadError("connection reset by the fake origin")


@dataclass
class FakeOrigin:
    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])
    mirror_blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])
    wrong: set[str] = field(default_factory=set[str])
    ignore_range: set[str] = field(default_factory=set[str])
    cut_once: dict[str, int] = field(default_factory=dict[str, int])
    status: dict[str, int] = field(default_factory=dict[str, int])
    redirects: dict[str, str] = field(default_factory=dict[str, str])
    json_docs: dict[str, object] = field(default_factory=dict[str, object])
    seen: list[Seen] = field(default_factory=list[Seen])

    def add(self, path: str, data: bytes, *, mirror: bool = True) -> str:
        """Serve ``data`` at ``ORIGIN/path`` (and on the mirror); return the URL."""
        url = f"{ORIGIN}/{path.lstrip('/')}"
        self.blobs[url] = data
        if mirror:
            self.mirror_blobs[sha256(data)] = data
        return url

    def urls(self) -> list[str]:
        return [s.url for s in self.seen]

    def _body(self, url: str) -> bytes | None:
        if url in self.blobs:
            data = self.blobs[url]
        elif url.startswith(f"{MIRROR}/sha256/"):
            data = self.mirror_blobs.get(url.rsplit("/", 1)[-1])
            if data is None:
                return None
        else:
            return None
        return b"X" * len(data) if url in self.wrong else data

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(Seen(url, dict(request.headers), request.extensions.get("timeout")))
        if url in self.redirects:
            return httpx.Response(302, headers={"location": self.redirects[url]})
        if url in self.status:
            return httpx.Response(self.status[url])
        if url in self.json_docs:
            return httpx.Response(200, json=self.json_docs[url])
        data = self._body(url)
        if data is None:
            return httpx.Response(404)
        start = 0
        wanted = request.headers.get("range", "")
        partial = wanted.startswith("bytes=") and url not in self.ignore_range
        if partial:
            start = int(wanted.removeprefix("bytes=").split("-", 1)[0])
            if start >= len(data):
                return httpx.Response(416, headers={"content-range": f"bytes */{len(data)}"})
        body = data[start:]
        headers = {"content-length": str(len(body))}
        code = 200
        if partial:
            code = 206
            headers["content-range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        limit = self.cut_once.pop(url, None)
        if limit is not None:
            return httpx.Response(code, headers=headers, stream=_Cut(body, limit))
        return httpx.Response(code, headers=headers, stream=httpx.ByteStream(body))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def definitions_text(
    rows: Mapping[str, Mapping[str, object]],
) -> str:
    """A ``packages.toml`` text: ``rows`` maps a package name to its keys; ``targets`` is a
    mapping of target name to a table of values."""
    lines: list[str] = []

    def value(v: object) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int | float):
            return str(v)
        if isinstance(v, list | tuple):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    for name, row in rows.items():
        lines.append(f"[packages.{name}]")
        for key, v in row.items():
            if key != "targets":
                lines.append(f"{key} = {value(v)}")
        lines.append(f"[packages.{name}.targets]")
        targets = row.get("targets", {})
        assert isinstance(targets, Mapping)
        for target, table in targets.items():
            assert isinstance(table, Mapping)
            inner = ", ".join(f"{k} = {value(v)}" for k, v in table.items())
            lines.append(f'"{target}" = {{ {inner} }}')
        lines.append("")
    return "\n".join(lines)
