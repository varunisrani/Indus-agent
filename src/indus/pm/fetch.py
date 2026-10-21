"""Verified, resumable downloads for pm (P24-23).

``Fetcher.fetch(url, sha256)`` returns ``<downloads>/<sha256>``, a file whose bytes hash to the
pinned value. Origins are tried in this order and every one is verified the same way:

1. the blob already in the download cache;
2. each local folder given (a ``bundle`` output: ``<folder>/blobs/<sha256>``), for offline
   installs;
3. the pinned URL;
4. the content-addressed mirror, ``<mirror>/sha256/<sha256>``, when one is configured.

A failure at one origin (HTTP error, network error after the retries, wrong hash) is recorded and
the next one is tried; when all fail, ``FetchFailed`` lists each origin with its reason. A hash
mismatch at the primary therefore falls back to the mirror, whose bytes must still verify.

Downloads land in ``<sha256>.part`` and resume: a later attempt asks for ``Range: bytes=N-`` and
appends when the answer is ``206`` starting at N, starts over on ``200``, and checks the part as
complete on ``416``. A resumed download whose hash is wrong is retried once from zero at the same
origin before moving on. Every request carries the configured timeout. Only ``https`` URLs are
fetched, redirects included (followed by hand, at most five hops).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from indus.pm.lock import PmError, valid_sha256
from indus.pm.store import file_sha256

__all__ = ["FetchFailed", "Fetcher", "HashMismatch", "mirror_url"]

USER_AGENT = "indus-pm/1"
MAX_REDIRECTS = 5
ATTEMPTS = 3
_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


class FetchFailed(PmError):
    """No origin produced the pinned bytes."""


class HashMismatch(PmError):
    """The bytes of one origin did not hash to the pinned value."""


class _Status(PmError):
    def __init__(self, code: int, url: str) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code
        self.url = url


class _Transport(PmError):
    """A network failure that a later attempt may resume past."""


def mirror_url(base: str, sha256: str) -> str:
    """The mirror's address for a blob: ``<base>/sha256/<hex>``."""
    if not valid_sha256(sha256):
        raise PmError("a mirror lookup needs a full lowercase sha256")
    return f"{base.rstrip('/')}/sha256/{sha256}"


def _https(url: str) -> bool:
    return urlsplit(url).scheme == "https"


class Fetcher:
    def __init__(
        self,
        downloads: Path,
        *,
        mirror: str = "",
        local_dirs: Sequence[Path] = (),
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        attempts: int = ATTEMPTS,
    ) -> None:
        self.downloads = downloads
        self.mirror = mirror.strip()
        self.local_dirs = tuple(local_dirs)
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self._transport = transport
        self._client: httpx.Client | None = None

    # -- plumbing ---------------------------------------------------------------------------------

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @contextmanager
    def _open(self, url: str, headers: dict[str, str]) -> Iterator[httpx.Response]:
        """A streamed response for ``url`` after following https-only redirects."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not _https(current):
                raise PmError(f"refusing non-https URL {current}")
            try:
                response = self.client().send(
                    self.client().build_request("GET", current, headers=headers), stream=True
                )
            except httpx.HTTPError as exc:
                raise _Transport(f"{type(exc).__name__}: {exc}") from exc
            if response.is_redirect:
                location = response.headers.get("location", "")
                response.close()
                current = urljoin(current, location)
                continue
            try:
                yield response
            finally:
                response.close()
            return
        raise PmError(f"too many redirects from {url}")

    # -- one origin -------------------------------------------------------------------------------

    def _download(self, url: str, part: Path) -> None:
        """Fill ``part`` from ``url``, resuming what is there (``_Status`` / ``_Transport``)."""
        last: Exception | None = None
        for _ in range(self.attempts):
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with self._open(url, headers) as response:
                    code = response.status_code
                    if code == 416 and have:
                        return
                    if code == 206 and have:
                        match = _RANGE.fullmatch(response.headers.get("content-range", ""))
                        if match is None or int(match.group(1)) != have:
                            part.unlink(missing_ok=True)
                            raise _Transport("the server answered a different byte range")
                        mode = "ab"
                    elif code == 200:
                        mode = "wb"
                    elif code in (408, 429) or code >= 500:
                        raise _Transport(f"HTTP {code}")
                    else:
                        raise _Status(code, url)
                    declared = response.headers.get("content-length")
                    written = 0
                    with part.open(mode) as out:
                        for chunk in response.iter_raw():
                            out.write(chunk)
                            written += len(chunk)
                    if declared is not None and declared.isdecimal() and written < int(declared):
                        raise _Transport(f"the connection ended after {written}/{declared} bytes")
                    return
            except httpx.HTTPError as exc:
                last = _Transport(f"{type(exc).__name__}: {exc}")
            except _Transport as exc:
                last = exc
        assert last is not None
        raise last

    def _from_url(self, url: str, sha256: str, blob: Path) -> None:
        part = blob.with_name(blob.name + ".part")
        resumed = part.exists() and part.stat().st_size > 0
        self._download(url, part)
        actual = file_sha256(part)
        if actual != sha256 and resumed:
            # The kept part may be what is wrong: one more try from zero at this origin.
            part.unlink(missing_ok=True)
            self._download(url, part)
            actual = file_sha256(part)
        if actual != sha256:
            part.unlink(missing_ok=True)
            raise HashMismatch(f"sha256 mismatch: pinned {sha256}, got {actual}")
        os.replace(part, blob)

    def _from_dir(self, folder: Path, sha256: str, blob: Path) -> None:
        source = folder / "blobs" / sha256
        if not source.is_file():
            raise PmError("not in this bundle")
        part = blob.with_name(blob.name + ".part")
        shutil.copyfile(source, part)
        actual = file_sha256(part)
        if actual != sha256:
            part.unlink(missing_ok=True)
            raise HashMismatch(f"sha256 mismatch: pinned {sha256}, got {actual}")
        os.replace(part, blob)

    # -- public -----------------------------------------------------------------------------------

    def blob_path(self, sha256: str) -> Path:
        return self.downloads / sha256

    def fetch(self, url: str, sha256: str) -> Path:
        """The verified blob for ``sha256`` (see the module notes for the origin order)."""
        if not valid_sha256(sha256):
            raise PmError(f"the pin for {url} has no full sha256", hint="run `indus pm lock`")
        self.downloads.mkdir(parents=True, exist_ok=True)
        blob = self.blob_path(sha256)
        if blob.is_file():
            if file_sha256(blob) == sha256:
                return blob
            blob.unlink(missing_ok=True)
        failures: list[str] = []
        for folder in self.local_dirs:
            try:
                self._from_dir(folder, sha256, blob)
                return blob
            except (PmError, OSError) as exc:
                failures.append(f"bundle {folder}: {exc}")
        origins = [("primary", url)]
        if self.mirror:
            origins.append(("mirror", mirror_url(self.mirror, sha256)))
        for label, candidate in origins:
            try:
                self._from_url(candidate, sha256, blob)
                return blob
            except PmError as exc:
                failures.append(f"{label} {candidate}: {exc}")
        raise FetchFailed(
            "could not fetch a verified copy:\n  " + "\n  ".join(failures),
            hint="check the network, or set pm.mirror_url to a mirror",
        )

    def hash_url(self, url: str) -> str:
        """Download ``url`` (no expected hash), keep it as a cached blob and return its sha256."""
        self.downloads.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        part = self.downloads / f".url-{key}.part"
        try:
            self._download(url, part)
        except _Status as exc:
            raise PmError(f"{url}: HTTP {exc.code}") from exc
        except _Transport as exc:
            raise PmError(f"{url}: {exc}") from exc
        sha = file_sha256(part)
        os.replace(part, self.blob_path(sha))
        return sha

    def get_json(self, url: str) -> object:
        """A small JSON document (release listings for ``indus pm update``)."""
        try:
            with self._open(url, {"Accept": "application/json"}) as response:
                if response.status_code != 200:
                    raise PmError(f"{url}: HTTP {response.status_code}")
                body = response.read()
        except _Transport as exc:
            raise PmError(f"{url}: {exc}") from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise PmError(f"{url}: not JSON") from exc
