"""Session titles: cleaning, uniqueness, provenance and ``name #N`` numbering.

A title is what pickers and ``--resume <name>`` show. ``sanitize`` keeps it printable and on one
line: control characters, zero-width and bidi-override characters (which can make text read
differently from what it is) and lone surrogates go, whitespace runs fold to one space, the result
is capped at 100 characters, and an empty result is NULL (clearing the title).

Titles are unique among titled rows (a unique index on non-NULL ``title``). Each carries a
provenance — ``user`` > ``llm`` > ``derived``; a titled row with NULL provenance counts as
``user``. A user write always lands; an automatic write lands only on an untitled row or over a
strictly lower rank. When the title is held by a compression ancestor of the session asking (its
own earlier segment), the ancestor gives it up inside the same transaction; any other holder makes
the write fail with ``TitleTaken``.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Literal, get_args

from indus.foundation.errors import StoreError
from indus.state import lineage
from indus.state.registry import StoreHandle

__all__ = [
    "MAX_TITLE_LENGTH",
    "RANKS",
    "TitleSource",
    "TitleTaken",
    "copy_title_in",
    "get_title",
    "next_lineage_title",
    "next_lineage_title_in",
    "resolve_title",
    "sanitize",
    "set_title",
    "set_title_in",
]

TitleSource = Literal["user", "llm", "derived"]
RANKS: dict[str, int] = {"derived": 1, "llm": 2, "user": 3}
MAX_TITLE_LENGTH = 100

# Invisible characters that change how text renders: zero-width space/joiners, word joiner and
# invisible operators, the BOM, every bidi embedding, override and isolate mark, and the
# interlinear-annotation and object-replacement placeholders. Listed as code points so no
# formatter turns them into literal invisible characters.
_INVISIBLE = frozenset(
    chr(point)
    for point in (
        *range(0x200B, 0x2010),
        0x061C,
        *range(0x2060, 0x2066),
        0xFEFF,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
        *range(0xFFF9, 0xFFFD),
    )
)
_SPACES = re.compile(r"\s+")
_NUMBERED = re.compile(r"^(?P<base>.*?) #(?P<n>\d+)$")


class TitleTaken(StoreError):
    """Another session (not a compression ancestor of this one) already has the title."""

    default_code = "store.title_taken"

    def __init__(self, message: str, *, holder: str = "") -> None:
        super().__init__(message)
        self.holder: str = holder


def _keep(char: str) -> bool:
    if char in _INVISIBLE:
        return False
    category = unicodedata.category(char)
    if category == "Cs":  # a lone surrogate
        return False
    # Control characters go, except whitespace ones, which the fold below turns into a space.
    return category != "Cc" or char.isspace()


def sanitize(text: str | None) -> str | None:
    """A printable one-line title of at most 100 characters, or None (which clears)."""
    if not text:
        return None
    kept = "".join(char for char in text if _keep(char))
    folded = _SPACES.sub(" ", kept).strip()
    folded = folded[:MAX_TITLE_LENGTH].rstrip()
    return folded or None


def _rank(source: object) -> int:
    if source is None:
        return RANKS["user"]
    return RANKS.get(str(source), 0)


def _yields_to(conn: sqlite3.Connection, holder: str, session_id: str) -> bool:
    """Is ``holder`` an earlier segment of the conversation ``session_id`` belongs to?"""
    return holder != session_id and holder in lineage.ancestors_in(conn, session_id)[:-1]


def set_title_in(
    conn: sqlite3.Connection, session_id: str, title: str | None, *, source: TitleSource
) -> bool:
    """Write the title under the provenance rules; True when the row now holds it."""
    if source not in RANKS:
        raise ValueError(f"unknown title source: {source!r}")
    clean = sanitize(title)
    row = conn.execute(
        "SELECT title, title_source FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return False
    if source != "user":
        if clean is None:
            return False  # only a person clears a title
        if row["title"] is not None and _rank(row["title_source"]) >= RANKS[source]:
            return False
    if clean is not None:
        holder = conn.execute(
            "SELECT id FROM sessions WHERE title = ? AND id <> ?", (clean, session_id)
        ).fetchone()
        if holder is not None:
            holder_id = str(holder["id"])
            if not _yields_to(conn, holder_id, session_id):
                raise TitleTaken(
                    f"the title {clean!r} is already used by session {holder_id}", holder=holder_id
                )
            conn.execute(
                "UPDATE sessions SET title = NULL, title_source = NULL WHERE id = ?", (holder_id,)
            )
    conn.execute(
        "UPDATE sessions SET title = ?, title_source = ? WHERE id = ?",
        (clean, source if clean is not None else None, session_id),
    )
    return True


def set_title(
    store: StoreHandle, session_id: str, title: str | None, *, source: TitleSource = "user"
) -> bool:
    return store.write(lambda conn: set_title_in(conn, session_id, title, source=source))


def copy_title_in(conn: sqlite3.Connection, from_session_id: str, to_session_id: str) -> bool:
    """Give ``to`` the title of ``from`` with the same provenance (rotation keeps authority)."""
    row = conn.execute(
        "SELECT title, title_source FROM sessions WHERE id = ?", (from_session_id,)
    ).fetchone()
    if row is None or row["title"] is None:
        return False
    stored = row["title_source"]
    source: TitleSource = stored if stored in get_args(TitleSource) else "user"
    return set_title_in(conn, to_session_id, row["title"], source=source)


def get_title(store: StoreHandle, session_id: str) -> str | None:
    with store.read() as conn:
        row = conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return None if row is None else row["title"]


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def next_lineage_title_in(conn: sqlite3.Connection, base: str) -> str:
    """``base`` without a trailing `` #N``, numbered one past the highest number in use.

    The plain title counts as #1, so ``name`` → ``name #2`` and ``x``, ``x #3`` → ``x #4``. A base
    nobody holds is returned as it is.
    """
    match = _NUMBERED.match(base)
    stem = match.group("base") if match else base
    rows = conn.execute(
        "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
        (stem, _like_escape(stem) + " #%"),
    ).fetchall()
    if not rows:
        return stem
    highest = 1
    for row in rows:
        found = _NUMBERED.match(str(row["title"]))
        if found and found.group("base") == stem:
            highest = max(highest, int(found.group("n")))
    return f"{stem} #{highest + 1}"


def next_lineage_title(store: StoreHandle, base: str) -> str:
    with store.read() as conn:
        return next_lineage_title_in(conn, base)


def resolve_title(store: StoreHandle, text: str) -> str | None:
    """The session a title names, preferring the newest ``text #N`` over the exact title."""
    clean = sanitize(text)
    if clean is None:
        return None
    with store.read() as conn:
        numbered = conn.execute(
            "SELECT id, title FROM sessions WHERE title LIKE ? ESCAPE '\\' "
            "ORDER BY started_at DESC, id DESC",
            (_like_escape(clean) + " #%",),
        ).fetchall()
        for row in numbered:
            found = _NUMBERED.match(str(row["title"]))
            if found and found.group("base") == clean:
                return str(row["id"])
        exact = conn.execute("SELECT id FROM sessions WHERE title = ?", (clean,)).fetchone()
    return None if exact is None else str(exact["id"])
