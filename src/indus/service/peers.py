"""Peer gateways: other Indus gateways a bot can message over their API server (P24-07).

A peer is a name, a base URL, an optional note and a ``lan`` flag, kept in ``<home>/peers.json``
(0600, written under a file lock by atomic rename). The file never holds a key: the peer's API key
lives in the profile's secret store under ``key_name(peer)`` = ``INDUS_PEER_<NAME>_KEY``, and the
same name is read from the profile's environment for headless use. The CLI (``indus peer``) and
the gateway (``message_agent``'s peer transport) share this module (layer rule D-16.246).

``PeerClient`` calls the peer's API server (P15-30) with ``Authorization: Bearer <key>``:

- ``ensure_chat`` → ``POST /v1/bots/chat`` (the peer resolves or creates its canonical chat);
- ``dm`` → ``POST /v1/bots/dm`` (one turn there, capped at ``RELAY_CAP_S`` = 10 minutes);
- ``run`` / ``status`` / ``stop`` → ``/v1/runs`` on the canonical chat's session.

A ``peer/profile`` target goes to the peer's ``/p/<profile>`` prefix (a multiplexed peer). The
client never follows a redirect (the key must not travel to another origin). Unless the peer was
added with ``lan``, every request passes the SSRF policy at connect time (``safe_client``: private,
loopback and link-local answers refused, metadata addresses always). Failures raise ``PeerError``
with a typed ``reason`` (``indus.state.bot_chat.FAILURE_REASONS``) and a message built from the
status, the peer's error code and our own words: never from the request, so the key cannot leak
into it.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import httpx

from indus.foundation import fsio
from indus.foundation.errors import IndusError
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.safety.safe_transport import ConnectBlocked, TransportFactory, safe_client
from indus.safety.url_policy import Resolver
from indus.state.bot_chat import FAILURE_REASONS, FailureReason, classify_failure

__all__ = [
    "CALL_TIMEOUT_S",
    "PEERS_FILE",
    "PEER_NAME_RE",
    "RELAY_CAP_S",
    "Peer",
    "PeerBook",
    "PeerClient",
    "PeerError",
    "PeerReply",
    "PeerTarget",
    "key_name",
    "parse_target",
    "peer_key",
    "validate_url",
]

PEERS_FILE: Final = "peers.json"
PEER_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROFILE_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
RELAY_CAP_S: Final = 600.0
"""A relayed cross-machine turn is given at most ten minutes."""
CALL_TIMEOUT_S: Final = 30.0
CONNECT_TIMEOUT_S: Final = 10.0
MAX_MESSAGE_CHARS: Final = 64_000
_FORMAT: Final = 1
_LOCK_TIMEOUT: Final = 10.0
_lock = threading.Lock()


class PeerError(IndusError):
    """A peer call that did not work; ``reason`` is the typed failure reason. ``accepted``: the
    peer took the message and its turn is still running (do not resend)."""

    default_code = "peers.failed"

    def __init__(
        self,
        message: str,
        *,
        reason: FailureReason = "unknown",
        status: int | None = None,
        accepted: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason: FailureReason = reason
        self.status = status
        self.accepted = accepted


# -- the registry -------------------------------------------------------------------------------


def key_name(peer: str) -> str:
    """The secret (and env) name of a peer's API key."""
    return "INDUS_PEER_" + re.sub(r"[^A-Z0-9]", "_", peer.upper()) + "_KEY"


def validate_url(text: str) -> str:
    """An ``http(s)`` base URL with a host and nothing that could carry a credential."""
    parts = urllib.parse.urlsplit(text.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PeerError("a peer URL is an http(s) base URL, e.g. http://host.lan:8642",
                        reason="missing_config")  # fmt: skip
    if parts.username is not None or parts.password is not None:
        raise PeerError("a peer URL must not hold a user name or password", reason="missing_config")
    if parts.query or parts.fragment:
        raise PeerError("a peer URL must not have a query or fragment", reason="missing_config")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class Peer:
    name: str
    url: str
    lan: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"url": self.url, "lan": self.lan}
        if self.note:
            out["note"] = self.note
        return out


class PeerBook:
    """One profile's registered peers (see the module notes)."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    @property
    def path(self) -> Path:
        return self.scope.home / PEERS_FILE

    def load(self) -> dict[str, Peer]:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return {}
        try:
            data: object = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        peers = cast(Mapping[str, object], data).get("peers") if isinstance(data, dict) else None
        if not isinstance(peers, dict):
            return {}
        out: dict[str, Peer] = {}
        for name, entry in cast(Mapping[str, object], peers).items():
            if not PEER_NAME_RE.fullmatch(name) or not isinstance(entry, dict):
                continue
            fields = cast(Mapping[str, object], entry)
            url = fields.get("url")
            if not isinstance(url, str):
                continue
            note = fields.get("note")
            out[name] = Peer(
                name, url, bool(fields.get("lan", False)), note if isinstance(note, str) else ""
            )
        return out

    def get(self, name: str) -> Peer | None:
        return self.load().get(name)

    def _save(self, peers: Mapping[str, Peer]) -> None:
        body = {"version": _FORMAT, "peers": {n: p.to_dict() for n, p in sorted(peers.items())}}
        fsio.atomic_write_json(self.path, body, mode=0o600, sort_keys=True)

    def _locked(self) -> fsio.FileLock:
        return fsio.FileLock(self.path.with_suffix(".lock"), timeout=_LOCK_TIMEOUT)

    def put(self, peer: Peer) -> bool:
        """Add or replace a peer; True when it was new."""
        if not PEER_NAME_RE.fullmatch(peer.name):
            raise PeerError(
                f"invalid peer name {peer.name!r}: lower-case letters, digits, '-' and '_'",
                reason="missing_config",
            )
        clean = Peer(peer.name, validate_url(peer.url), peer.lan, peer.note.strip())
        with _lock, self._locked():
            peers = self.load()
            new = clean.name not in peers
            peers[clean.name] = clean
            self._save(peers)
        return new

    def remove(self, name: str) -> bool:
        with _lock, self._locked():
            peers = self.load()
            if peers.pop(name, None) is None:
                return False
            self._save(peers)
        return True


def peer_key(scope: Scope, peer: str) -> str | None:
    """The peer's API key: the profile's secret store first, then its environment."""
    name = key_name(peer)
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception:  # an unreadable store must not stop the env fallback
            value = None
        if value and value.strip():
            return value.strip()
    value = scope.env.get(name)
    return value.strip() if value and value.strip() else None


# -- targets ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PeerTarget:
    peer: str
    profile: str | None = None

    def render(self) -> str:
        return self.peer if self.profile is None else f"{self.peer}/{self.profile}"


def parse_target(text: str) -> PeerTarget:
    """``peer`` or ``peer/profile``."""
    peer, _, profile = text.strip().partition("/")
    peer = peer.strip().lower()
    if not PEER_NAME_RE.fullmatch(peer):
        raise PeerError(f"invalid peer name {peer!r}", reason="missing_config")
    clean = profile.strip() or None
    if clean is not None and not PROFILE_RE.fullmatch(clean):
        raise PeerError(f"invalid profile name {clean!r}", reason="missing_config")
    return PeerTarget(peer, clean)


# -- the client ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class PeerReply:
    session_id: str
    reply: str


def _status_reason(status: int) -> FailureReason:
    if status in (401, 403):
        return "provider_auth"
    if status == 404:
        return "missing_config"
    if status in (409, 423):
        return "target_busy"
    if status == 429:
        return "provider_rate_limit"
    if status == 504:
        return "delivery_timeout"
    if status >= 500:
        return "provider_server"
    return "unknown"


class PeerClient:
    """Calls to registered peers (see the module notes).

    ``transport`` and ``resolver`` exist for tests: the transport is used for ``lan`` peers and
    as the inner transport behind the SSRF check for the others; the resolver answers host names.
    """

    def __init__(
        self,
        scope: Scope,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.scope = scope
        self.book = PeerBook(scope)
        self._transport = transport
        self._resolver = resolver

    def _resolve(self, target: PeerTarget) -> tuple[Peer, str, str]:
        peer = self.book.get(target.peer)
        if peer is None:
            raise PeerError(
                f"no peer named {target.peer!r}; add it with `indus peer add {target.peer} "
                "--url URL --key-stdin`",
                reason="missing_config",
            )
        key = peer_key(self.scope, peer.name)
        if not key:
            raise PeerError(
                f"no API key for peer {peer.name!r}; store it with `indus peer add {peer.name} "
                f"--url {peer.url} --key-stdin` or set {key_name(peer.name)}",
                reason="missing_config",
            )
        base = peer.url
        if target.profile is not None:
            base += "/p/" + urllib.parse.quote(target.profile, safe="")
        return peer, base, key

    def _client(self, peer: Peer, timeout: float) -> httpx.AsyncClient:
        limits = httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT_S))
        if peer.lan:
            return httpx.AsyncClient(
                transport=self._transport or httpx.AsyncHTTPTransport(),
                trust_env=False,
                follow_redirects=False,
                timeout=limits,
            )
        inner = self._transport
        factory: TransportFactory | None = None if inner is None else (lambda _proxy: inner)
        return safe_client(
            self.scope,
            resolver=self._resolver,
            inner_factory=factory,
            follow_redirects=False,
            timeout=limits,
        )

    async def request(
        self,
        target: PeerTarget,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        timeout: float = CALL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """One call; the JSON object answered, or ``PeerError``."""
        peer, base, key = self._resolve(target)
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "indus-peer",
        }
        name = target.render()
        sent = False
        try:
            async with self._client(peer, timeout) as client:
                request = client.build_request(
                    method, base + path, json=dict(body) if body is not None else None,
                    headers=headers,
                )  # fmt: skip
                sent = True
                response = await client.send(request)
                raw = await response.aread()
        except ConnectBlocked:
            raise PeerError(
                f"the network policy refuses peer {name}'s address; add it with --lan if it is "
                "on your own network",
                reason="missing_config",
            ) from None
        except httpx.ConnectTimeout:
            raise PeerError(
                f"peer {name} did not answer the connection", reason="runtime_offline"
            ) from None
        except httpx.ConnectError:
            raise PeerError(f"could not reach peer {name}", reason="runtime_offline") from None
        except httpx.TimeoutException:
            raise PeerError(
                f"peer {name} took the message but did not answer within {int(timeout)} s; its "
                "turn may still be running there: do not resend",
                reason="delivery_timeout",
                accepted=sent,
            ) from None
        except httpx.HTTPError as exc:
            raise PeerError(
                f"the call to peer {name} failed ({type(exc).__name__})", reason="runtime_offline"
            ) from None
        return self._decode(name, response.status_code, raw)

    @staticmethod
    def _decode(name: str, status: int, raw: bytes) -> dict[str, Any]:
        data: object
        try:
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            data = None
        if 300 <= status < 400:
            raise PeerError(
                f"peer {name} answered with a redirect ({status}); redirects are not followed",
                status=status,
            )
        if status >= 400 or not isinstance(data, dict):
            error: Mapping[str, object] = {}
            if isinstance(data, dict):
                found = cast(Mapping[str, object], data).get("error")
                if isinstance(found, dict):
                    error = cast(Mapping[str, object], found)
            given = error.get("reason")
            code = error.get("code")
            known: dict[str, FailureReason] = {r: r for r in FAILURE_REASONS}
            reason: FailureReason = (
                known[given]
                if isinstance(given, str) and given in known
                else _status_reason(status)
                if status >= 400
                else "unknown"
            )
            if reason == "unknown":
                text = error.get("message")
                reason = classify_failure(text if isinstance(text, str) else None)
            what = f" ({code})" if isinstance(code, str) and code else ""
            detail = error.get("message")
            said = (
                ": " + redact_text(detail)[:300]
                if isinstance(detail, str) and detail.strip()
                else ""
            )
            if status < 400:
                raise PeerError(f"peer {name} answered something that is not a JSON object")
            raise PeerError(
                f"peer {name} refused the request (HTTP {status}{what}){said}",
                reason=reason,
                status=status,
            )
        return cast(dict[str, Any], data)

    # -- calls -------------------------------------------------------------------------------------

    async def ensure_chat(self, target: PeerTarget) -> str:
        data = await self.request(target, "POST", "/v1/bots/chat", body={})
        session = data.get("session_id")
        if not isinstance(session, str) or not session:
            raise PeerError(f"peer {target.render()} did not answer a session id")
        return session

    async def dm(self, target: PeerTarget, message: str, *, author: str | None = None) -> PeerReply:
        """One turn in the peer's canonical chat; its reply (empty for a silent turn)."""
        if not message.strip():
            raise PeerError("the message is empty", reason="unknown")
        if len(message) > MAX_MESSAGE_CHARS:
            raise PeerError(f"the message is longer than {MAX_MESSAGE_CHARS:,} characters")
        body: dict[str, Any] = {"message": message}
        if author:
            body["author"] = author
        data = await self.request(
            target, "POST", "/v1/bots/dm", body=body, timeout=RELAY_CAP_S + CALL_TIMEOUT_S
        )
        reply = data.get("reply")
        return PeerReply(str(data.get("session_id") or ""), reply if isinstance(reply, str) else "")

    async def run(self, target: PeerTarget, message: str) -> dict[str, Any]:
        """Start a long turn in the peer's canonical chat; answers its run id at once."""
        session = await self.ensure_chat(target)
        data = await self.request(
            target, "POST", "/v1/runs", body={"input": message, "session_id": session}
        )
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise PeerError(f"peer {target.render()} did not answer a run id")
        return {"run_id": run_id, "session_id": session, "status": data.get("status", "started")}

    async def status(self, target: PeerTarget, run_id: str) -> dict[str, Any]:
        return await self.request(target, "GET", "/v1/runs/" + urllib.parse.quote(run_id, safe=""))

    async def stop(self, target: PeerTarget, run_id: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(run_id, safe="")
        return await self.request(target, "POST", f"/v1/runs/{quoted}/stop", body={})
