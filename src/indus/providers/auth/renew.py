"""Renewing a stored sign-in once, however many sessions and processes want it at the same moment.

Some providers hand out single-use refresh tokens and revoke the whole sign-in when one is spent
twice (OpenAI does). So a renewal holds two locks: one per credential in this process, and a file
lock (``<home>/auth-refresh.lock``) across processes. Under them it reads the stored tokens again:

- the stored access token is no longer the one the caller saw → another session or process renewed
  it meanwhile, and that pair is used as it is (the spent refresh token is never sent again);
- otherwise the refresh grant is sent, the new pair is stored in the SecretStore and the pool row's
  ``expires_at`` follows.

Everything runs on one worker thread (the file lock belongs to the thread that took it), the HTTP
call on its own event loop there. Errors keep ``device_code``'s types (``ReloginRequired``,
``RefreshThrottled``, ``DeviceFlowError``); none carries a token.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from indus.foundation import fsio
from indus.foundation.clock import Clock
from indus.providers.auth import device_code
from indus.providers.auth.device_code import DeviceFlowSpec, TokenSet
from indus.providers.auth.pool_model import secret_name

log = logging.getLogger(__name__)

LOCK_FILE = "auth-refresh.lock"
LOCK_TIMEOUT_SECONDS = device_code.HTTP_TIMEOUT_SECONDS + 15.0

type ClientFactory = Callable[[], httpx.AsyncClient]

_guards: dict[str, threading.Lock] = {}
_guards_lock = threading.Lock()


class TokenSink(Protocol):
    def get(self, name: str, /) -> str | None: ...

    def set(self, name: str, value: str, /) -> None: ...


def _guard(key: str) -> threading.Lock:
    with _guards_lock:
        found = _guards.get(key)
        if found is None:
            found = _guards[key] = threading.Lock()
        return found


def _note_expiry(home: Path, provider: str, entry_id: str, tokens: TokenSet) -> None:
    """Keep the pool row's ``expires_at`` in step (metadata only; best effort)."""
    from indus.providers.auth.store import AuthStore

    expires = tokens.expires_at.isoformat() if tokens.expires_at else None
    try:
        with AuthStore(home).transaction() as doc:
            rows = cast(dict[str, Any], doc.get("pool") or {}).get(provider)
            if isinstance(rows, list):
                for row in cast(list[object], rows):
                    if isinstance(row, dict) and cast(dict[str, Any], row).get("id") == entry_id:
                        cast(dict[str, Any], row)["expires_at"] = expires
    except Exception as exc:
        log.debug("could not note the renewed %s sign-in's expiry: %s", provider, exc)


def renew_stored(
    *,
    home: Path,
    secrets: TokenSink,
    provider: str,
    entry_id: str,
    spec: DeviceFlowSpec,
    clock: Clock,
    seen_access: str | None,
    make_client: ClientFactory | None = None,
) -> TokenSet | None:
    """Renew the sign-in stored for ``entry_id`` (blocking; see the module notes).

    ``seen_access`` is the access token the caller holds: when the store has another one, it is
    returned without a refresh. None when nothing is stored (signed out meanwhile)."""
    name = secret_name(provider, entry_id)
    factory = make_client or device_code.make_client
    with _guard(f"{home}\0{name}"), fsio.FileLock(home / LOCK_FILE, timeout=LOCK_TIMEOUT_SECONDS):
        stored = secrets.get(name)
        if not stored:
            return None
        try:
            tokens = TokenSet.from_secret(stored)
        except (ValueError, KeyError, TypeError):
            raise device_code.ReloginRequired(
                f"the stored {provider} sign-in is damaged; sign in again"
            ) from None
        if seen_access is not None and tokens.access_token != seen_access:
            log.info("%s sign-in was renewed by another session; using that one", provider)
            return tokens

        async def go() -> TokenSet:
            async with factory() as client:
                return await device_code.refresh_tokens(client, spec, tokens, clock=clock)

        fresh = asyncio.run(go())
        secrets.set(name, fresh.to_secret())
    _note_expiry(home, provider, entry_id, fresh)
    log.info("%s sign-in renewed", provider)
    return fresh
