"""Message search: query sanitising, routing between the indexes and LIKE, hits with context.

``search(store, query, ...)`` never hands raw text to FTS5 — its query grammar raises on stray
specials — so the text is sanitised first (``sanitize``). The route then depends on the query
and on the store:

1. an explicit ``role="tool"`` search scans canonical rows with LIKE (tool rows are only partly
   indexed, and the whole stored body must be searchable);
2. a stale index (fail-open, P02-14) or a store without FTS uses LIKE with a compiled subset of
   the boolean syntax (OR groups of ANDed terms, ``NOT`` on the next term);
3. a CJK query uses the trigram index when every term has at least 3 characters, else LIKE with
   one OR'd clause per term;
4. any other query uses the word index; with no hits it tries the trigram index (substrings such
   as ``ttpcl`` in ``httpclient``) when every term has at least 3 characters, then an
   OR-relaxed retry of an implicit-AND query with two or more terms.

Live and compaction-archived rows are searchable; rewound rows only with ``include_inactive``.
Snippets mark the match with ``[[`` and ``]]`` on every route. With ``context=True`` each hit
carries its neighbour before and after in the same session (id order), cut at 200 characters,
fetched in one batch. A search slower than ``sessions.search_slow_ms`` (default 1000; 0 logs
every search) logs its route.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from indus.state import fts
from indus.state.errors import is_fts_scoped_corruption

if TYPE_CHECKING:
    from indus.state.registry import StoreHandle

__all__ = [
    "CONTEXT_CHARS",
    "MAX_QUERY_CHARS",
    "SLOW_MS_DEFAULT",
    "ContextLine",
    "Hit",
    "has_cjk",
    "or_relaxed",
    "sanitize",
    "search",
    "terms_of",
]

log = logging.getLogger(__name__)

MAX_QUERY_CHARS = 2048
CONTEXT_CHARS = 200
SNIPPET_TOKENS = 24
SNIPPET_OPEN = "[["
SNIPPET_CLOSE = "]]"
ELLIPSIS = "…"
LIKE_BEFORE = 60
LIKE_WINDOW = 220
IN_CHUNK = 900
SLOW_MS_DEFAULT = 1000.0
TRIGRAM_MIN_TERM = 3

Sort = Literal["rank", "newest", "oldest"]

_OPERATORS = frozenset({"AND", "OR", "NOT"})
_UNIT = re.compile(r'"[^"]*"\*?|\S+')
_INNER_STAR = re.compile(r"\*+(?=[^\s*])")
_CJK_BLOCKS = (
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x2E80, 0x2FDF),  # CJK radicals
    (0x3000, 0x303F),  # CJK symbols and punctuation
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3130, 0x318F),  # Hangul compatibility Jamo
    (0x31F0, 0x31FF),  # Katakana extensions
    (0x3400, 0x4DBF),  # Extension A
    (0x4E00, 0x9FFF),  # Unified ideographs
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # Compatibility ideographs
    (0xFF66, 0xFF9F),  # Half-width Katakana
    (0x20000, 0x3134F),  # Extensions B..G
)


@dataclass(frozen=True)
class ContextLine:
    role: str
    text: str


@dataclass(frozen=True)
class Hit:
    message_id: int
    session_id: str
    role: str
    snippet: str
    timestamp: float
    tool_name: str | None
    source: str
    model: str | None
    session_started: float
    context: list[ContextLine] | None = None


# -- query text ---------------------------------------------------------------------------------


def _clean_char(ch: str) -> str:
    code = ord(ch)
    if 0xD800 <= code <= 0xDFFF:
        return "�"
    if code < 32 or code == 127:
        return " "
    return ch


def _is_word_char(ch: str) -> bool:
    """A character FTS5 accepts inside a bare term."""
    return ord(ch) > 127 or ch.isalnum() or ch == "_"


def _split_phrases(text: str) -> Iterator[tuple[bool, str]]:
    """``(is_phrase, text)`` pieces; quotes pair left to right, a lone quote is dropped."""
    rest = text
    while rest:
        start = rest.find('"')
        if start < 0:
            yield False, rest
            return
        if start:
            yield False, rest[:start]
        end = rest.find('"', start + 1)
        if end < 0:
            yield False, rest[start + 1 :]
            return
        yield True, rest[start + 1 : end]
        rest = rest[end + 1 :]


def _shape_term(part: str) -> str | None:
    """One bare piece (word characters, ``-``, ``.``, ``*``) as an FTS5 term, or None."""
    core = part.rstrip("*")
    prefix = core != part
    core = core.strip("-.")
    if not any(_is_word_char(ch) for ch in core):
        return None
    if "-" in core or "." in core or core in _OPERATORS:
        # The tokenizer splits a dotted word; as a phrase its parts stay together. An operator
        # word that the user did not write as an operator is a plain word too.
        core = f'"{core}"'
    return core + ("*" if prefix else "")


def _bare_terms(text: str) -> Iterator[str]:
    for chunk in text.split():
        if chunk in _OPERATORS:
            yield chunk
            continue
        kept = "".join(ch if _is_word_char(ch) or ch in "-.*" else " " for ch in chunk)
        kept = _INNER_STAR.sub(" ", kept)
        for part in kept.split():
            term = _shape_term(part)
            if term is not None:
                yield term


def _tidy_operators(tokens: list[str]) -> list[str]:
    """Drop operators at the start, at the end and right after another operator."""
    out: list[str] = []
    for token in tokens:
        if token in _OPERATORS and (not out or out[-1] in _OPERATORS):
            continue
        out.append(token)
    while out and out[-1] in _OPERATORS:
        out.pop()
    return out


def sanitize(query: str) -> str:
    """A query FTS5 can parse: see the module docstring. Empty when nothing searchable is left."""
    text = "".join(_clean_char(ch) for ch in query[:MAX_QUERY_CHARS])
    tokens: list[str] = []
    for is_phrase, piece in _split_phrases(text):
        if is_phrase:
            if piece.strip():
                tokens.append(f'"{piece}"')
            continue
        tokens.extend(_bare_terms(piece))
    return " ".join(_tidy_operators(tokens))


def _units(query: str) -> list[str]:
    return _UNIT.findall(query)


def _term_text(unit: str) -> str:
    return unit.rstrip("*").strip('"').strip()


def terms_of(query: str) -> list[str]:
    """The non-operator terms of a sanitised query, without quotes or prefix stars."""
    return [t for t in (_term_text(u) for u in _units(query) if u not in _OPERATORS) if t]


def or_relaxed(query: str) -> str | None:
    """An implicit-AND query rewritten as any-term OR; None for one term or explicit OR/NOT."""
    units = _units(query)
    if "OR" in units or "NOT" in units:
        return None
    terms = [u for u in units if u != "AND"]
    return " OR ".join(terms) if len(terms) >= 2 else None


def has_cjk(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _CJK_BLOCKS)


def _quoted_terms(query: str) -> str:
    """Every term as a quoted string (operators kept): the trigram index's query form."""
    out: list[str] = []
    for unit in _units(query):
        if unit in _OPERATORS:
            out.append(unit)
            continue
        term = _term_text(unit)
        if term:
            out.append('"' + term.replace('"', '""') + '"')
    return " ".join(_tidy_operators(out))


# -- SQL pieces ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Filters:
    sources: tuple[str, ...] | None
    exclude_sources: tuple[str, ...] | None
    roles: tuple[str, ...]
    started_after: float | None
    started_before: float | None
    include_inactive: bool

    def clauses(self) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if not self.include_inactive:
            where.append("(m.active = 1 OR m.compacted = 1)")
        if self.sources is not None:
            where.append(_in_sql("s.source", self.sources, params))
        if self.exclude_sources:
            where.append("NOT " + _in_sql("s.source", self.exclude_sources, params))
        if self.roles:
            where.append(_in_sql("m.role", self.roles, params))
        if self.started_after is not None:
            where.append("s.started_at >= ?")
            params.append(float(self.started_after))
        if self.started_before is not None:
            where.append("s.started_at < ?")
            params.append(float(self.started_before))
        return where, params


def _in_sql(column: str, values: Sequence[str], params: list[Any]) -> str:
    """``column IN (...)``, chunked at 900 variables per list (R03.18); empty matches nothing."""
    if not values:
        return "0"
    groups: list[str] = []
    for start in range(0, len(values), IN_CHUNK):
        chunk = values[start : start + IN_CHUNK]
        groups.append(f"{column} IN ({', '.join('?' for _ in chunk)})")
        params.extend(chunk)
    return "(" + " OR ".join(groups) + ")"


_ROW_COLUMNS = (
    "m.id AS message_id, m.session_id AS session_id, m.role AS role, m.timestamp AS timestamp, "
    "m.tool_name AS tool_name, s.source AS source, s.model AS model, "
    "s.started_at AS session_started"
)


def _order_sql(sort: Sort, *, ranked: bool) -> str:
    if sort == "oldest":
        return "ORDER BY m.timestamp ASC, m.id ASC"
    if sort == "newest" or not ranked:
        return "ORDER BY m.timestamp DESC, m.id DESC"
    return "ORDER BY rank, m.id DESC"


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_LIKE_ANY = (
    "(COALESCE(m.body, '') LIKE ? ESCAPE '\\' OR COALESCE(m.tool_name, '') LIKE ? ESCAPE '\\' "
    "OR COALESCE(m.tool_calls, '') LIKE ? ESCAPE '\\')"
)


def _like_groups(query: str) -> list[list[tuple[str, bool]]]:
    """OR-separated groups of ``(term, negated)``; groups without a positive term are dropped."""
    groups: list[list[tuple[str, bool]]] = [[]]
    negate = False
    for unit in _units(query):
        if unit == "OR":
            if groups[-1]:
                groups.append([])
            negate = False
            continue
        if unit == "AND":
            continue
        if unit == "NOT":
            negate = True
            continue
        term = _term_text(unit)
        if term:
            groups[-1].append((term, negate))
            negate = False
    return [g for g in groups if any(not negated for _, negated in g)]


# -- result shaping -----------------------------------------------------------------------------


def _mark(text: str, terms: Sequence[str]) -> str:
    """Wrap every case-insensitive occurrence of any term in the snippet markers."""
    wanted = sorted({t for t in terms if t}, key=len, reverse=True)
    if not wanted or not text:
        return text
    pattern = re.compile("|".join(re.escape(t) for t in wanted), re.IGNORECASE)
    return pattern.sub(lambda m: f"{SNIPPET_OPEN}{m.group(0)}{SNIPPET_CLOSE}", text)


def _like_snippet(row: sqlite3.Row, terms: Sequence[str]) -> str:
    excerpt = row["excerpt"]
    if excerpt is None:
        return _mark(str(row["tool_name"] or ""), terms)
    start = int(row["excerpt_start"])
    total = int(row["body_length"] or 0)
    text = str(excerpt)
    head = ELLIPSIS if start > 1 else ""
    tail = ELLIPSIS if start - 1 + len(text) < total else ""
    return head + _mark(text, terms) + tail


def _hit(row: sqlite3.Row, snippet: str) -> Hit:
    return Hit(
        message_id=int(row["message_id"]),
        session_id=str(row["session_id"]),
        role=str(row["role"]),
        snippet=snippet,
        timestamp=float(row["timestamp"]),
        tool_name=None if row["tool_name"] is None else str(row["tool_name"]),
        source=str(row["source"]),
        model=None if row["model"] is None else str(row["model"]),
        session_started=float(row["session_started"]),
    )


# -- routes -------------------------------------------------------------------------------------


@dataclass
class _Plan:
    store: StoreHandle
    filters: _Filters
    sort: Sort
    limit: int
    offset: int
    route: str = "empty"
    failure: BaseException | None = field(default=None)


def _match(plan: _Plan, table: str, match: str) -> list[Hit] | None:
    """Hits from one FTS table; None when the query cannot run there. FTS-scoped corruption is
    kept on the plan (handled after the read connection is given back)."""
    where, params = plan.filters.clauses()
    sql = (
        f"SELECT {_ROW_COLUMNS}, snippet({table}, -1, '{SNIPPET_OPEN}', '{SNIPPET_CLOSE}', "
        f"'{ELLIPSIS}', {SNIPPET_TOKENS}) AS snip "
        f"FROM {table} JOIN messages AS m ON m.id = {table}.rowid "
        "JOIN sessions AS s ON s.id = m.session_id "
        f"WHERE {' AND '.join([f'{table} MATCH ?', *where])} "
        f"{_order_sql(plan.sort, ranked=True)} LIMIT ? OFFSET ?"
    )
    try:
        with plan.store.read() as conn:
            rows = conn.execute(sql, [match, *params, plan.limit, plan.offset]).fetchall()
    except sqlite3.DatabaseError as exc:
        if is_fts_scoped_corruption(exc):
            plan.failure = exc
            return None
        if isinstance(exc, sqlite3.OperationalError):
            log.debug("search query %r could not run on %s: %s", match, table, exc)
            return None
        raise
    return [_hit(row, str(row["snip"] or "")) for row in rows]


def _like(plan: _Plan, groups: list[list[tuple[str, bool]]]) -> list[Hit]:
    if not groups:
        return []
    params: list[Any] = []
    ors: list[str] = []
    for group in groups:
        ands: list[str] = []
        for term, negated in group:
            ands.append(("NOT " if negated else "") + _LIKE_ANY)
            params.extend([f"%{_escape_like(term)}%"] * 3)
        ors.append("(" + " AND ".join(ands) + ")")
    positive = [term for group in groups for term, negated in group if not negated]
    where, filter_params = plan.filters.clauses()
    body = fts.text_sql("x")
    sql = (
        f"SELECT {_ROW_COLUMNS}, "
        "max(1, instr(lower(m.body), lower(?)) - ?) AS excerpt_start, "
        "substr(m.body, max(1, instr(lower(m.body), lower(?)) - ?), ?) AS excerpt, "
        "length(m.body) AS body_length "
        "FROM (SELECT x.id, x.session_id, x.role, x.timestamp, x.tool_name, x.tool_calls, "
        f"x.active, x.compacted, {body} AS body FROM messages AS x) AS m "
        "JOIN sessions AS s ON s.id = m.session_id "
        f"WHERE {' AND '.join(['(' + ' OR '.join(ors) + ')', *where])} "
        f"{_order_sql(plan.sort, ranked=False)} LIMIT ? OFFSET ?"
    )
    anchor = positive[0]
    head = [anchor, LIKE_BEFORE, anchor, LIKE_BEFORE, LIKE_WINDOW]
    with plan.store.read() as conn:
        rows = conn.execute(sql, [*head, *params, *filter_params, plan.limit, plan.offset])
        found = rows.fetchall()
    return [_hit(row, _like_snippet(row, positive)) for row in found]


def _after_failure(plan: _Plan, query: str) -> list[Hit]:
    """An index raised FTS-scoped corruption: detach it, then answer from canonical rows."""
    failure, plan.failure = plan.failure, None
    if failure is not None:
        fts.fail_open(plan.store, failure)
    plan.route = "like_after_index_damage"
    return _like(plan, _like_groups(query))


def _route(plan: _Plan, query: str) -> list[Hit]:
    if "tool" in plan.filters.roles:
        plan.route = "like_tool"
        return _like(plan, _like_groups(query))
    with plan.store.read() as conn:
        stale = fts.is_stale(conn)
        found, plan.failure = fts.probe(conn)
    usable = set(found)
    if plan.failure is not None and not stale:
        return _after_failure(plan, query)
    if stale or fts.BASE not in usable:
        plan.route = "like_stale" if stale else "like_no_index"
        return _like(plan, _like_groups(query))
    sources = plan.filters.sources
    trigram_ok = fts.TRIGRAM in usable and not (
        sources is not None and set(sources) & set(fts.TRIGRAM_EXCLUDED_SOURCES)
    )
    terms = terms_of(query)
    long_terms = bool(terms) and all(len(t) >= TRIGRAM_MIN_TERM for t in terms)
    if has_cjk(query):
        if trigram_ok and long_terms:
            plan.route = "trigram"
            hits = _match(plan, fts.TRIGRAM, _quoted_terms(query))
            if plan.failure is not None:
                return _after_failure(plan, query)
            if hits is not None:
                return hits
        plan.route = "like_cjk"
        return _like(plan, [[(t, False)] for t in terms])
    plan.route = "words"
    hits = _match(plan, fts.BASE, query)
    if plan.failure is not None:
        return _after_failure(plan, query)
    if hits:
        return hits
    if trigram_ok and long_terms:
        plan.route = "trigram"
        hits = _match(plan, fts.TRIGRAM, _quoted_terms(query))
        if plan.failure is not None:
            return _after_failure(plan, query)
        if hits:
            return hits
    relaxed = or_relaxed(query)
    if relaxed is not None:
        plan.route = "words_any"
        hits = _match(plan, fts.BASE, relaxed)
        if plan.failure is not None:
            return _after_failure(plan, query)
        if hits:
            return hits
    return []


# -- context ------------------------------------------------------------------------------------


def _attach_context(plan: _Plan, hits: list[Hit]) -> list[Hit]:
    visible = "" if plan.filters.include_inactive else " AND (n.active = 1 OR n.compacted = 1)"
    neighbours: dict[int, list[ContextLine]] = {hit.message_id: [] for hit in hits}
    ids = list(neighbours)
    body = fts.text_sql("c")
    for start in range(0, len(ids), IN_CHUNK):
        chunk = ids[start : start + IN_CHUNK]
        sql = (
            f"SELECT t.id AS hit_id, c.role AS role, substr({body}, 1, ?) AS text "
            "FROM messages AS t JOIN messages AS c ON c.id IN ("
            "(SELECT n.id FROM messages AS n WHERE n.session_id = t.session_id "
            f"AND n.id < t.id{visible} ORDER BY n.id DESC LIMIT 1), "
            "(SELECT n.id FROM messages AS n WHERE n.session_id = t.session_id "
            f"AND n.id > t.id{visible} ORDER BY n.id ASC LIMIT 1)) "
            f"WHERE t.id IN ({', '.join('?' for _ in chunk)}) ORDER BY t.id, c.id"
        )
        try:
            with plan.store.read() as conn:
                rows = conn.execute(sql, [CONTEXT_CHARS, *chunk]).fetchall()
        except sqlite3.Error as exc:  # context is a nicety; the hits stand without it
            log.warning("could not load the context of %d search hits: %s", len(chunk), exc)
            continue
        for row in rows:
            text = "" if row["text"] is None else str(row["text"])[:CONTEXT_CHARS]
            neighbours[int(row["hit_id"])].append(ContextLine(str(row["role"]), text))
    return [replace(hit, context=neighbours.get(hit.message_id, [])) for hit in hits]


# -- entry --------------------------------------------------------------------------------------


def _slow_ms(store: StoreHandle) -> float:
    config = store.scope.config
    raw = config.get("sessions.search_slow_ms") if config is not None else None
    if raw is None or isinstance(raw, bool):
        return SLOW_MS_DEFAULT
    try:
        value = float(str(raw))
    except ValueError:
        return SLOW_MS_DEFAULT
    return value if value >= 0 else SLOW_MS_DEFAULT


def _as_tuple(value: str | Sequence[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def search(
    store: StoreHandle,
    query: str,
    *,
    sources: Sequence[str] | None = None,
    exclude_sources: Sequence[str] | None = None,
    role: str | Sequence[str] | None = None,
    started_after: float | None = None,
    started_before: float | None = None,
    include_inactive: bool = False,
    sort: Sort = "rank",
    limit: int = 20,
    offset: int = 0,
    context: bool = False,
) -> list[Hit]:
    """Search messages; see the module docstring for routing and visibility."""
    if sort not in ("rank", "newest", "oldest"):
        raise ValueError(f"sort must be rank, newest or oldest, not {sort!r}")
    if limit < 0 or offset < 0:
        raise ValueError("limit and offset must not be negative")
    filters = _Filters(
        sources=_as_tuple(sources),
        exclude_sources=_as_tuple(exclude_sources),
        roles=_as_tuple(role) or (),
        started_after=started_after,
        started_before=started_before,
        include_inactive=include_inactive,
    )
    plan = _Plan(store, filters, sort, limit, offset)
    clock = store.scope.clock
    started = clock.monotonic()
    hits: list[Hit] | None = None
    try:
        cleaned = sanitize(query)
        if not cleaned or limit == 0:
            hits = []
            return hits
        hits = _route(plan, cleaned)
        if context and hits:
            hits = _attach_context(plan, hits)
        return hits
    finally:
        elapsed_ms = (clock.monotonic() - started) * 1000.0
        if elapsed_ms >= _slow_ms(store):
            log.info(
                "slow session search: route=%s elapsed=%.0fms hits=%s query=%r",
                plan.route,
                elapsed_ms,
                "error" if hits is None else len(hits),
                query[:200],
            )
