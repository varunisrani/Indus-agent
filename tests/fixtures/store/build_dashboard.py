"""Builds the dashboard's session store fixture (P18-11, P18-13) in a test's profile home.

``build_dashboard_store(scope)`` writes, through the real P02 writers, one session for each
source the Sessions page tells apart (chats from the CLI, TUI, web and a messaging platform;
automation from cron, a tool, a one-shot run, the API server and ACP), with messages that the
search index covers, usage counters, one session whose turn is running (a live turn lease), one
whose turn was lost in a restart (an ``unknown`` admission), one archived, one long-ended and quiet
(for prune), one ended and empty (for delete-empty) and one still open but quiet. It returns the
ids by role so tests do not repeat them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from indus.foundation.scope import Scope
from indus.state import messages, registry, sessions
from indus.state.registry import StoreHandle

DAY = 86400.0

CHAT_SOURCES = ("cli", "tui", "web", "telegram")
AUTOMATION_SOURCES = ("cron", "tool", "oneshot", "api_server", "acp")


@dataclass
class DashboardStore:
    path: Path
    now: float
    by_source: dict[str, str] = field(default_factory=dict[str, str])
    busy: str = "s-busy"
    unknown: str = "s-lost"
    archived: str = "s-archived"
    old: str = "s-old"
    empty: str = "s-empty"
    quiet_open: str = "s-quiet-open"
    searchable: str = "s-cli"

    @property
    def chats(self) -> list[str]:
        return [self.by_source[s] for s in CHAT_SOURCES]

    @property
    def automation(self) -> list[str]:
        return [self.by_source[s] for s in AUTOMATION_SOURCES]


def _new(
    store: StoreHandle,
    sid: str,
    source: str,
    *,
    started_at: float,
    title: str | None = None,
    turns: tuple[tuple[str, str], ...] = (),
    tokens: tuple[int, int] = (0, 0),
) -> None:
    sessions.create(store, sid, source, started_at=started_at, model="fake-model")
    for role, text in turns:
        messages.append(store, sid, {"role": role, "content": text, "timestamp": started_at + 5})

    def stamp(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE sessions SET title = ?, input_tokens = ?, output_tokens = ?, "
            "last_activity_at = ? WHERE id = ?",
            (title, tokens[0], tokens[1], started_at + 10, sid),
        )

    store.write(stamp)


def _end(store: StoreHandle, sid: str, at: float) -> None:
    sessions.end(store, sid, "user_exit")
    store.write(
        lambda conn: conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (at, sid))
    )


def build_dashboard_store(scope: Scope) -> DashboardStore:
    now = scope.clock.now().timestamp()
    with registry.open_unshared(scope) as store:
        out = DashboardStore(path=store.path, now=now)
        for offset, source in enumerate((*CHAT_SOURCES, *AUTOMATION_SOURCES)):
            sid = f"s-{source.replace('_', '-')}"
            out.by_source[source] = sid
            text = f"Please summarise the {source} report about blue herons"
            _new(
                store,
                sid,
                source,
                started_at=now - 3600 + offset * 60,
                title=f"{source.title()} session",
                turns=(("user", text), ("assistant", f"Here is the {source} summary.")),
                tokens=(100 + offset, 10 + offset),
            )
        out.searchable = out.by_source["cli"]
        _new(store, out.busy, "cli", started_at=now - 600, turns=(("user", "busy work"),))
        store.write(
            lambda conn: conn.execute(
                "INSERT INTO turn_leases(conversation_id, holder, acquired_at, expires_at, "
                "generation) VALUES (?, 'worker-1', ?, ?, 1)",
                (out.busy, now - 30, now + 600),
            )
        )
        _new(store, out.unknown, "cli", started_at=now - 900, turns=(("user", "lost work"),))
        payload = json.dumps({"text": "lost work"})
        store.write(
            lambda conn: conn.execute(
                "INSERT INTO runtime_admissions(admission_id, principal_id, session_id, "
                "request_id, payload_json, digest, state, owner_epoch, generation, created_at, "
                "updated_at) VALUES ('adm-lost', 'p', ?, 'r1', ?, 'd', 'unknown', 1, 1, ?, ?)",
                (out.unknown, payload, now - 900, now - 900),
            )
        )
        _new(store, out.archived, "cli", started_at=now - 7200, turns=(("user", "kept away"),))
        sessions.set_archived(store, out.archived, True)
        _new(store, out.old, "cli", started_at=now - 40 * DAY, turns=(("user", "ancient"),))
        _end(store, out.old, now - 40 * DAY + 60)
        _new(store, out.empty, "cli", started_at=now - 3 * DAY)
        _end(store, out.empty, now - 3 * DAY + 5)
        _new(store, out.quiet_open, "cli", started_at=now - 50 * DAY, turns=(("user", "idle"),))
        return out
