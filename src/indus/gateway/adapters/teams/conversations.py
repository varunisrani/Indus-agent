"""Teams conversation ids, service URLs and the stored conversation references (P16-11).

A Bot Connector request goes to the ``serviceUrl`` an activity arrived with, carrying a bot token,
so that URL decides where the token goes. ``service_url_ok`` accepts only HTTPS on the default
port to an exact host on the Microsoft list (plus hosts the operator adds): a tampered value can
neither send the token elsewhere nor reach an internal address (SSRF).

Conversation ids end up in URL paths. ``conversation_ok`` keeps them to the characters Teams uses
(``a:1…``, ``19:…@thread.tacv2``, ``29:…``) with an optional ``;messageid=<digits>`` suffix, which
is how a reply chain in a channel is addressed; ``split_conversation`` / ``join_conversation``
move between that form and our (chat id, thread id) pair.

``ConversationBook`` remembers, per conversation, the service URL, type and tenant the last
verified activity carried, in ``<home>/gateway/teams_conversations.json`` (0600, newest 2,000), so
a cron job or ``indus send`` in another process can write into a conversation proactively.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import urllib.parse
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from indus.foundation.blocking import run_blocking
from indus.foundation.fsio import atomic_write_json

__all__ = [
    "BOOK_CAP",
    "DEFAULT_SERVICE_URL",
    "SERVICE_HOSTS",
    "ConversationBook",
    "ConversationRef",
    "activity_id_ok",
    "conversation_ok",
    "join_conversation",
    "service_url_ok",
    "split_conversation",
]

log = logging.getLogger(__name__)

DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"
# Exact hosts only: anyone can register a name under trafficmanager.net.
SERVICE_HOSTS = frozenset(
    {
        "smba.trafficmanager.net",
        "smba.infra.gov.teams.microsoft.us",
        "smba.infra.dod.teams.microsoft.us",
    }
)
BOOK_CAP = 2000

_BASE_ID = re.compile(r"^[A-Za-z0-9:@._-]{1,400}$")
_THREAD_ID = re.compile(r"^[0-9]{1,32}$")
_ACTIVITY_ID = re.compile(r"^[A-Za-z0-9:@._|-]{1,200}$")
_THREAD_MARK = ";messageid="
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


def service_url_ok(url: str, extra_hosts: Iterable[str] = ()) -> str | None:
    """``url`` with a trailing slash when it is an allowed Bot Connector base, else None."""
    if not isinstance(url, str) or not url or len(url) > 512:  # pyright: ignore[reportUnnecessaryIsInstance]
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port not in (None, 443):
        return None
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        return None
    host = (parts.hostname or "").lower()
    allowed = SERVICE_HOSTS | {h.strip().lower() for h in extra_hosts if h.strip()}
    if host not in allowed:
        return None
    path = parts.path or "/"
    if ".." in path or "//" in path or "\\" in path:
        return None
    return f"https://{host}{path if path.endswith('/') else path + '/'}"


def valid_host(host: str) -> bool:
    return bool(_HOST.match(host.strip().lower()))


def conversation_ok(conversation_id: str) -> bool:
    base, thread = split_conversation(conversation_id)
    return bool(_BASE_ID.match(base)) and (thread is None or bool(_THREAD_ID.match(thread)))


def activity_id_ok(activity_id: str) -> bool:
    return bool(_ACTIVITY_ID.match(activity_id))


def split_conversation(conversation_id: str) -> tuple[str, str | None]:
    """``19:x@thread.tacv2;messageid=17`` → (``19:x@thread.tacv2``, ``17``)."""
    base, mark, thread = conversation_id.partition(_THREAD_MARK)
    return base, (thread if mark else None)


def join_conversation(chat_id: str, thread_id: str | None) -> str:
    base, _ = split_conversation(chat_id)
    return f"{base}{_THREAD_MARK}{thread_id}" if thread_id else base


@dataclass(frozen=True)
class ConversationRef:
    conversation_id: str
    service_url: str
    kind: str = ""
    tenant_id: str = ""
    name: str = ""


class ConversationBook:
    """Conversation references by base conversation id (see the module notes)."""

    def __init__(
        self, path: Path | None, *, cap: int = BOOK_CAP, extra_hosts: Iterable[str] = ()
    ) -> None:
        self.path = path
        self.cap = cap
        self.extra_hosts = tuple(extra_hosts)
        self._refs: OrderedDict[str, ConversationRef] = OrderedDict()
        self._loaded = False
        self._dirty = False
        self._saver: asyncio.Task[None] | None = None

    async def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None:
            return
        stored = await run_blocking(_read, self.path, self.extra_hosts, label="teams conversations")
        for ref in stored:
            if ref.conversation_id not in self._refs:
                self._refs[ref.conversation_id] = ref
        self._trim()

    def get(self, conversation_id: str) -> ConversationRef | None:
        base, _ = split_conversation(conversation_id)
        return self._refs.get(base)

    def remember(self, ref: ConversationRef) -> None:
        base, _ = split_conversation(ref.conversation_id)
        ref = ConversationRef(base, ref.service_url, ref.kind, ref.tenant_id, ref.name)
        if self._refs.get(base) == ref:
            self._refs.move_to_end(base)
            return
        self._refs[base] = ref
        self._refs.move_to_end(base)
        self._trim()
        self._dirty = True
        if self.path is not None and (self._saver is None or self._saver.done()):
            self._saver = asyncio.ensure_future(self._save_loop())

    def __len__(self) -> int:
        return len(self._refs)

    def _trim(self) -> None:
        while len(self._refs) > self.cap:
            self._refs.popitem(last=False)

    async def _save_loop(self) -> None:
        while self._dirty and self.path is not None:
            self._dirty = False
            snapshot = [asdict(ref) for ref in self._refs.values()]
            try:
                await run_blocking(_write, self.path, snapshot, label="teams conversations")
            except Exception as exc:
                log.warning("teams: conversation references not saved: %s", type(exc).__name__)
                return

    async def flush(self) -> None:
        saver = self._saver
        if saver is not None:
            with contextlib.suppress(Exception):
                await saver
        if self._dirty and self.path is not None:
            await self._save_loop()


def _read(path: Path, extra_hosts: tuple[str, ...]) -> list[ConversationRef]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("teams: conversation references unreadable: %s", type(exc).__name__)
        return []
    if not isinstance(raw, list):
        return []
    found: list[ConversationRef] = []
    for item in raw:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
        conversation = str(entry.get("conversation_id") or "")
        service = service_url_ok(str(entry.get("service_url") or ""), extra_hosts)
        if not conversation_ok(conversation) or service is None:
            continue
        found.append(
            ConversationRef(
                conversation,
                service,
                str(entry.get("kind") or ""),
                str(entry.get("tenant_id") or ""),
                str(entry.get("name") or "")[:200],
            )
        )
    return found


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, rows, mode=0o600)
