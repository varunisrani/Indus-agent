"""Loading a media source the model named: a web address, a ``data:`` URL or a local file.

- ``http``/``https``: the URL policy runs first (``check_url``: private, metadata and blocklisted
  hosts, credentials in the URL) so a refused address is never contacted; the download then goes
  through ``safe_client``, which re-checks every request and redirect hop and dials only vetted
  addresses. The body is read in pieces and abandoned past ``max_bytes``; the whole download has
  ``timeout_s``.
- ``data:``: must be base64; its encoded length is checked against ``max_bytes`` before decoding.
- anything else is a path (``file://`` and a leading ``~`` allowed; relative to ``cwd``). It is
  resolved through symlinks, then refused when it is a device, a secret store, an Indus control
  file or a secret-bearing ``.env`` file (the ``read_file`` rules).

``Loaded.key`` names the source for counters: a resolved path, a URL without its fragment, or a
digest of a data URL.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.digits import is_digits
from indus.foundation.scope import Scope
from indus.safety.path_guards import blocked_device, read_denial
from indus.safety.safe_transport import ConnectBlocked, TransportFactory, safe_client
from indus.safety.url_policy import Blocked, Resolver, check_url_async

__all__ = ["Loaded", "SourceError", "load_source", "source_key"]

USER_AGENT = "IndusAgent/0 (+media)"
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


class SourceError(ValueError):
    """The source could not be loaded; the message is meant for the model."""


@dataclass(frozen=True)
class Loaded:
    data: bytes
    key: str
    origin: str  # "url", "data" or "file"
    label: str  # what to call it in messages


def source_key(src: str, cwd: Path | None = None) -> str:
    text = src.strip()
    if text.startswith("data:"):
        return "data:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]
    if text.lower().startswith(("http://", "https://")):
        return "url:" + text.split("#", 1)[0]
    return "file:" + str(_path_of(text, None, cwd))


def _path_of(text: str, scope: Scope | None, cwd: Path | None) -> Path:
    if text.lower().startswith("file://"):
        text = text[len("file://") :]
    if text.startswith("~"):
        home = scope.env.get("HOME") if scope is not None else None
        if home:
            text = home + text[1:]
    path = Path(text)
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    return Path(os.path.realpath(path))


def _too_big(max_bytes: int) -> str:
    return f"the file is larger than the {max_bytes // (1024 * 1024)} MB limit"


def _from_data_url(text: str, max_bytes: int) -> bytes:
    header, _, payload = text.partition(",")
    if ";base64" not in header.lower():
        raise SourceError("a data: URL must be base64-encoded (data:<type>;base64,...)")
    if len(payload) * 3 // 4 > max_bytes:
        raise SourceError(_too_big(max_bytes))
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SourceError("the data: URL holds invalid base64") from exc


def _read_file(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SourceError(_too_big(max_bytes))
    return data


async def _download(
    url: str,
    scope: Scope | None,
    *,
    max_bytes: int,
    timeout_s: float,
    resolver: Resolver | None,
    inner_factory: TransportFactory | None,
) -> bytes:
    verdict = await check_url_async(url, scope, resolver=resolver)
    if isinstance(verdict, Blocked):
        raise SourceError(f"the address was refused: {verdict.reason}")
    client = safe_client(
        scope,
        resolver=resolver,
        inner_factory=inner_factory,
        timeout=timeout_s,
        headers={"User-Agent": USER_AGENT, "Accept": "image/*,audio/*,video/*,*/*;q=0.5"},
    )
    body = bytearray()
    try:
        async with client, client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise SourceError(f"the server answered HTTP {response.status_code}")
            declared = response.headers.get("content-length", "")
            if is_digits(declared) and int(declared) > max_bytes:
                raise SourceError(_too_big(max_bytes))
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise SourceError(_too_big(max_bytes))
    except ConnectBlocked as exc:
        raise SourceError(f"the address was refused: {exc.verdict.reason}") from exc
    except httpx.TimeoutException as exc:
        raise SourceError(f"the download did not finish within {timeout_s:g} s") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"the download failed: {type(exc).__name__}: {exc}") from exc
    return bytes(body)


async def load_source(
    src: str,
    scope: Scope | None,
    *,
    max_bytes: int,
    timeout_s: float = 30.0,
    cwd: Path | None = None,
    resolver: Resolver | None = None,
    inner_factory: TransportFactory | None = None,
) -> Loaded:
    """The bytes of ``src`` under the rules in the module notes; ``SourceError`` otherwise."""
    text = (src or "").strip()
    if not text:
        raise SourceError("no source was given")
    if text.startswith("data:"):
        return Loaded(_from_data_url(text, max_bytes), source_key(text), "data", "the data: URL")
    if text.lower().startswith(("http://", "https://")):
        data = await _download(
            text,
            scope,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
            resolver=resolver,
            inner_factory=inner_factory,
        )
        return Loaded(data, source_key(text), "url", text[:200])
    if _SCHEME.match(text) and not text.lower().startswith("file://") and not _is_drive(text):
        raise SourceError(
            "unsupported source; give an http(s) URL, a local file path, a file:// URI or a "
            "data: URL"
        )
    path = _path_of(text, scope, cwd)
    if blocked_device(str(path)):
        raise SourceError(f"{path} is a device, not a file")
    denial = read_denial(str(path), scope)
    if denial:
        raise SourceError(denial)
    if not path.is_file():
        raise SourceError(f"no file at {path}")
    data = await run_blocking(_read_file, path, max_bytes, label="media source read")
    return Loaded(data, "file:" + str(path), "file", str(path))


def _is_drive(text: str) -> bool:
    return len(text) >= 3 and text[1] == ":" and text[2] in "/\\"
