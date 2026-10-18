"""The wire copy of the history and the send-time sanitizer (R01.1, R01.2).

A model call never sends the durable history. ``build_wire_copy`` makes fresh message dicts from
transcript rows: a user or assistant row's ``api_content`` sidecar (the bytes it was sent with
before) stands in for ``content``, and storage-only fields (uids, row ids, display kind, scaffold
flag, finish reason, timestamps) stay behind. ``sanitize_wire`` then shapes that copy for the
route, in this order:

1. drop messages whose role the route does not take;
2. heal empty non-final user/assistant messages with a fixed placeholder (counted in a
   ``HealLedger``; the last message is never touched);
3. drop ``tool_calls`` that are empty or not a list;
4. coerce tool names to ``^[A-Za-z0-9_-]{1,64}$``;
5. drop tool results without an id;
6. rename a call id already used earlier in the payload to ``<id>_d<n>`` (smallest free ``n``
   from 1) and move the matching result of the run right after it along; earlier ids are never
   rewritten, so appending a turn never changes the bytes before it;
7. pair calls and results positionally: a result outside its call's run, or a second result for
   one call, is dropped; an unanswered call gets a fixed stub result at the end of its run;
8. realign each result's ``name`` with its call's function name;
9. drop thinking-only assistant messages, then merge same-role neighbours that leaves (or that
   arrived unrepaired);
10. trim string content, scrub lone surrogates;
11. canonicalise tool-call argument JSON (sorted keys, compact separators) through a bounded memo.

Every step is deterministic: the same history always becomes the same bytes. The input is never
mutated (it is structurally cloned first).

``finish_request`` is the very last step before a request leaves for one route: it applies that
route's prompt-cache plan (``core.cache_plan``) or strips every marker for a route that takes none
(P08-07).
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from indus.core import cache_plan
from indus.core.transcript import TranscriptRow, call_id, content_has_payload
from indus.providers.types import ChatRequest
from indus.state.messages import scrub_surrogates

__all__ = [
    "DEFAULT_MEMO",
    "EMPTY_PLACEHOLDER",
    "FALLBACK_TOOL_NAME",
    "HEAL_NOTICE_AFTER",
    "MEMO_MAX_BYTES",
    "MEMO_MAX_ENTRIES",
    "STUB_RESULT",
    "ArgsMemo",
    "HealLedger",
    "WireRules",
    "build_wire_copy",
    "canonical_arguments",
    "coerce_tool_name",
    "finish_request",
    "prepare_wire",
    "repair_arguments",
    "rules_for",
    "sanitize_wire",
]

type Message = dict[str, Any]

EMPTY_PLACEHOLDER = "[no reply was recorded for this turn]"
STUB_RESULT = "[no result was recorded for this tool call]"
FALLBACK_TOOL_NAME = "unnamed_tool"
HEAL_NOTICE_AFTER = 3
MEMO_MAX_ENTRIES = 4096
MEMO_MAX_BYTES = 32 * 1024 * 1024

_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NOT_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_UNDERSCORES = re.compile(r"_{2,}")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
# Whole string literals (skipped), single brackets, or a lone quote that opens an unclosed string.
_JSON_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]]|"', re.DOTALL)
_CLOSER_OF = {"{": "}", "[": "]"}

_BASE_ROLES = frozenset({"system", "user", "assistant", "tool"})
_ROLES_BY_MODE: dict[str, frozenset[str]] = {
    "chat_completions": _BASE_ROLES | {"developer"},
    "anthropic_messages": _BASE_ROLES,
}


# -- rules ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WireRules:
    """What a route accepts. ``positional_pairing``: results must sit right after their call."""

    roles: frozenset[str] = _BASE_ROLES
    positional_pairing: bool = True
    drop_thinking_only: bool = True


def rules_for(route: object) -> WireRules:
    """Rules for ``route`` (a ``RuntimeRoute``, a ``WireRules`` or ``None``).

    A profile may carry its own ``wire_rules``; otherwise the roles follow the API mode and both
    strict steps stay on.
    """
    if isinstance(route, WireRules):
        return route
    profile = getattr(route, "profile", None)
    declared = getattr(profile, "wire_rules", None)
    if isinstance(declared, WireRules):
        return declared
    mode = getattr(route, "api_mode", "")
    return WireRules(roles=_ROLES_BY_MODE.get(mode if isinstance(mode, str) else "", _BASE_ROLES))


# -- heal ledger ---------------------------------------------------------------------------------


@dataclass
class HealLedger:
    """Per-session count of placeholder heals; asks for one notice when heals keep happening."""

    events: int = 0
    healed: int = 0
    notice_after: int = HEAL_NOTICE_AFTER
    notified: bool = False

    def record(self, healed: int) -> None:
        if healed > 0:
            self.events += 1
            self.healed += healed

    def take_notice(self) -> bool:
        """True exactly once, the first time ``events`` reaches ``notice_after`` (≤ 0: never)."""
        if self.notified or self.notice_after <= 0 or self.events < self.notice_after:
            return False
        self.notified = True
        return True


# -- argument canonicalisation -------------------------------------------------------------------


def _canonical_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ArgsMemo:
    """Raw argument text → canonical text, bounded by entries and by bytes (oldest out first)."""

    def __init__(self, max_entries: int = MEMO_MAX_ENTRIES, max_bytes: int = MEMO_MAX_BYTES):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._map: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._map)

    def __contains__(self, raw: object) -> bool:
        return raw in self._map

    @property
    def bytes(self) -> int:
        return self._bytes

    def clear(self) -> None:
        self._map.clear()
        self._bytes = 0

    def canonical(self, raw: str) -> str:
        """Canonical form of ``raw``; raises ``ValueError`` when it is not valid JSON."""
        hit = self._map.get(raw)
        if hit is not None:
            self._map.move_to_end(raw)
            return hit[0]
        out = _canonical_dump(json.loads(raw))
        size = len(raw.encode("utf-8", "surrogatepass")) + len(out.encode("utf-8", "surrogatepass"))
        self._map[raw] = (out, size)
        self._bytes += size
        while self._map and (
            len(self._map) > self.max_entries
            or (self._bytes > self.max_bytes and len(self._map) > 1)
        ):
            _, (_, gone) = self._map.popitem(last=False)
            self._bytes -= gone
        return out


DEFAULT_MEMO = ArgsMemo()


def _close_brackets(text: str) -> str | None:
    """Append the closers a cut-off JSON text is missing; ``None`` when it ends inside a string."""
    pending: list[str] = []
    for token in _JSON_TOKENS.finditer(text):
        piece = token.group()
        if piece == '"':  # a quote whose string never closes
            return None
        if piece in _CLOSER_OF:
            pending.append(_CLOSER_OF[piece])
        elif piece in "}]" and pending and pending[-1] == piece:
            pending.pop()
    return text + "".join(reversed(pending))


def repair_arguments(raw: str) -> str:
    """Best-effort canonical JSON for broken argument text; ``{}`` when nothing parses."""
    text = raw.strip()
    if not text or text in ("None", "null"):
        return "{}"
    candidates = [text]
    no_commas = _TRAILING_COMMA.sub(r"\1", text)
    candidates.append(no_commas)
    closed = _close_brackets(no_commas)
    if closed is not None:
        candidates.append(_TRAILING_COMMA.sub(r"\1", closed))
    for candidate in candidates:
        try:
            return _canonical_dump(json.loads(candidate, strict=False))
        except ValueError:
            continue
    return "{}"


def canonical_arguments(raw: object, memo: ArgsMemo | None = None) -> str:
    """Canonical argument JSON; broken text is repaired (not memoised), non-text becomes ``{}``."""
    if not isinstance(raw, str):
        return "{}" if raw is None else _canonical_dump(raw)
    try:
        return (memo if memo is not None else DEFAULT_MEMO).canonical(raw)
    except ValueError:
        return repair_arguments(raw)


def coerce_tool_name(name: object) -> str:
    """A provider-safe tool name; a valid name comes back unchanged."""
    if not isinstance(name, str):
        return FALLBACK_TOOL_NAME
    if _TOOL_NAME.fullmatch(name):
        return name
    coerced = _UNDERSCORES.sub("_", _NOT_NAME_CHARS.sub("_", name.strip())).strip("_")
    return coerced[:64] or FALLBACK_TOOL_NAME


# -- helpers -------------------------------------------------------------------------------------


def _clone(value: Any) -> Any:
    """Structural copy of JSON-shaped data (dicts and lists copied, leaves shared)."""
    if isinstance(value, dict):
        return {k: _clone(v) for k, v in cast(dict[Any, Any], value).items()}
    if isinstance(value, list):
        return [_clone(v) for v in cast(list[Any], value)]
    if isinstance(value, tuple):
        return [_clone(v) for v in cast(tuple[Any, ...], value)]
    return value


def _calls(msg: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = msg.get("tool_calls")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in cast(list[object], raw):
        if isinstance(item, dict):
            out.append(cast(dict[str, Any], item))
    return out


def _has_payload(msg: Mapping[str, Any]) -> bool:
    reasoning = msg.get("reasoning")
    return bool(
        content_has_payload(msg.get("content"))
        or _calls(msg)
        or (isinstance(reasoning, str) and reasoning.strip())
        or msg.get("reasoning_details")
    )


def _thinking_only(msg: Mapping[str, Any]) -> bool:
    if msg.get("role") != "assistant" or _calls(msg) or content_has_payload(msg.get("content")):
        return False
    reasoning = msg.get("reasoning")
    return bool((isinstance(reasoning, str) and reasoning.strip()) or msg.get("reasoning_details"))


def _result_id(msg: Mapping[str, Any]) -> str:
    value = msg.get("tool_call_id")
    return value.strip() if isinstance(value, str) else ""


def _as_parts(content: object) -> list[Any]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return list(cast(list[Any], content))
    return [content]


def _join(first: object, second: object, glue: str) -> object:
    if not content_has_payload(second):
        return first if first is not None else second
    if not content_has_payload(first):
        return second
    if isinstance(first, str) and isinstance(second, str):
        return f"{first}{glue}{second}"
    return _as_parts(first) + _as_parts(second)


# -- the steps -----------------------------------------------------------------------------------


def _drop_roles(msgs: list[Message], rules: WireRules) -> list[Message]:
    return [m for m in msgs if m.get("role") in rules.roles]


def _heal_empty(msgs: list[Message]) -> int:
    healed = 0
    for msg in msgs[:-1]:
        if msg.get("role") in ("user", "assistant") and not _has_payload(msg):
            msg["content"] = EMPTY_PLACEHOLDER
            healed += 1
    return healed


def _drop_empty_call_arrays(msgs: list[Message]) -> None:
    for msg in msgs:
        if "tool_calls" not in msg:
            continue
        calls = _calls(msg)
        if msg.get("role") != "assistant" or not calls:
            del msg["tool_calls"]
        else:
            msg["tool_calls"] = calls


def _coerce_names(msgs: list[Message]) -> None:
    for msg in msgs:
        for call in _calls(msg):
            function = call.get("function")
            if not isinstance(function, dict):
                call["function"] = {"name": FALLBACK_TOOL_NAME, "arguments": "{}"}
                continue
            fn = cast(dict[str, Any], function)
            fn["name"] = coerce_tool_name(fn.get("name"))


def _drop_idless_results(msgs: list[Message]) -> list[Message]:
    return [m for m in msgs if m.get("role") != "tool" or _result_id(m)]


def _free_suffix(base: str, taken: set[str]) -> str:
    n = 1
    while f"{base}_d{n}" in taken:
        n += 1
    return f"{base}_d{n}"


def _rename_repeated_ids(msgs: list[Message]) -> None:
    taken: set[str] = set()
    pending: dict[str, deque[str]] = {}
    for index, msg in enumerate(msgs):
        role = msg.get("role")
        if role == "assistant":
            pending = {}
            for position, call in enumerate(_calls(msg)):
                original = call_id(call)
                base = original or f"call_{index}_{position}"
                new = base if base not in taken else _free_suffix(base, taken)
                taken.add(new)
                if call.get("id") != new:
                    call["id"] = new
                    if "call_id" in call:
                        call["call_id"] = new
                if original:
                    pending.setdefault(original, deque()).append(new)
        elif role == "tool":
            queue = pending.get(_result_id(msg))
            if queue:
                new = queue.popleft()
                if msg.get("tool_call_id") != new:
                    msg["tool_call_id"] = new
        else:
            pending = {}


def _stub_for(cid: str, call: Mapping[str, Any]) -> Message:
    function = call.get("function")
    name = cast(Mapping[str, Any], function).get("name") if isinstance(function, Mapping) else None
    stub: Message = {"role": "tool", "tool_call_id": cid, "content": STUB_RESULT}
    if isinstance(name, str):
        stub["name"] = name
    return stub


def _pair_positionally(msgs: list[Message]) -> list[Message]:
    out: list[Message] = []
    open_calls: dict[str, dict[str, Any]] = {}

    def close_run() -> None:
        for cid, call in open_calls.items():
            out.append(_stub_for(cid, call))
        open_calls.clear()

    for msg in msgs:
        role = msg.get("role")
        if role == "tool":
            cid = _result_id(msg)
            if cid in open_calls:
                del open_calls[cid]
                out.append(msg)
            continue
        close_run()
        if role == "assistant":
            for call in _calls(msg):
                cid = call_id(call)
                if cid:
                    open_calls[cid] = call
        out.append(msg)
    close_run()
    return out


def _drop_unknown_results(msgs: list[Message]) -> list[Message]:
    """Loose routes: only results whose id no call in the payload declares are dropped."""
    declared = {call_id(c) for m in msgs for c in _calls(m)}
    return [m for m in msgs if m.get("role") != "tool" or _result_id(m) in declared]


def _realign_names(msgs: list[Message]) -> None:
    names: dict[str, str] = {}
    for msg in msgs:
        for call in _calls(msg):
            function = call.get("function")
            name = (
                cast(Mapping[str, Any], function).get("name")
                if isinstance(function, Mapping)
                else None
            )
            cid = call_id(call)
            if cid and isinstance(name, str) and name:
                names[cid] = name
    for msg in msgs:
        if msg.get("role") == "tool" and "name" in msg:
            expected = names.get(_result_id(msg))
            if expected and msg.get("name") != expected:
                msg["name"] = expected


def _merge_neighbours(msgs: list[Message]) -> list[Message]:
    out: list[Message] = []
    for msg in msgs:
        prev = out[-1] if out else None
        role = msg.get("role")
        if prev is None or prev.get("role") != role or role not in ("user", "assistant"):
            out.append(msg)
            continue
        if role == "user":
            prev["content"] = _join(prev.get("content"), msg.get("content"), "\n\n")
            continue
        prev["content"] = _join(prev.get("content"), msg.get("content"), "\n")
        calls = _calls(prev) + _calls(msg)
        if calls:
            prev["tool_calls"] = calls
        if not prev.get("reasoning") and msg.get("reasoning"):
            prev["reasoning"] = msg["reasoning"]
        details = list(prev.get("reasoning_details") or []) + list(
            msg.get("reasoning_details") or []
        )
        if details:
            prev["reasoning_details"] = details
    return out


def _tidy_text(msgs: list[Message], memo: ArgsMemo | None) -> None:
    for msg in msgs:
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = scrub_surrogates(content).strip()
        elif isinstance(content, list):
            for part in cast(list[object], content):
                if isinstance(part, dict):
                    block = cast(dict[str, Any], part)
                    text = block.get("text")
                    if isinstance(text, str):
                        block["text"] = scrub_surrogates(text)
        reasoning = msg.get("reasoning")
        if isinstance(reasoning, str):
            msg["reasoning"] = scrub_surrogates(reasoning)
        for call in _calls(msg):
            function = call.get("function")
            if isinstance(function, dict):
                fn = cast(dict[str, Any], function)
                fn["arguments"] = canonical_arguments(fn.get("arguments"), memo)


# -- public --------------------------------------------------------------------------------------


def build_wire_copy(rows: Iterable[TranscriptRow], route: object = None) -> list[Message]:
    """Fresh message dicts for a call (sidecar substituted, storage-only fields left out).

    ``route`` is accepted for symmetry with ``sanitize_wire``; per-route shaping happens there and
    in the transports (which also decide what to do with the reasoning fields).
    """
    out: list[Message] = []
    for row in rows:
        msg: Message = {"role": row.role, "content": _clone(row.content)}
        sidecar = row.api_content
        if row.role in ("user", "assistant") and isinstance(sidecar, str) and sidecar:
            msg["content"] = sidecar
        if row.tool_calls:
            msg["tool_calls"] = _clone(row.tool_calls)
        if row.role == "tool":
            if row.tool_call_id is not None:
                msg["tool_call_id"] = row.tool_call_id
            if row.name is not None:
                msg["name"] = row.name
        if row.reasoning is not None:
            msg["reasoning"] = row.reasoning
        if row.reasoning_details:
            msg["reasoning_details"] = _clone(row.reasoning_details)
        out.append(msg)
    return out


def sanitize_wire(
    messages: Sequence[Mapping[str, Any]],
    route: object = None,
    *,
    heals: HealLedger | None = None,
    memo: ArgsMemo | None = None,
) -> list[Message]:
    """A route-safe copy of ``messages`` (see the module notes for the steps and their order)."""
    rules = rules_for(route)
    msgs: list[Message] = [_clone(dict(m)) for m in messages]
    msgs = _drop_roles(msgs, rules)
    healed = _heal_empty(msgs)
    if heals is not None:
        heals.record(healed)
    _drop_empty_call_arrays(msgs)
    _coerce_names(msgs)
    msgs = _drop_idless_results(msgs)
    _rename_repeated_ids(msgs)
    msgs = _pair_positionally(msgs) if rules.positional_pairing else _drop_unknown_results(msgs)
    _realign_names(msgs)
    if rules.drop_thinking_only:
        msgs = [m for m in msgs if not _thinking_only(m)]
    msgs = _merge_neighbours(msgs)
    _tidy_text(msgs, memo)
    return msgs


def prepare_wire(
    rows: Iterable[TranscriptRow],
    route: object = None,
    *,
    heals: HealLedger | None = None,
    memo: ArgsMemo | None = None,
) -> list[Message]:
    """``sanitize_wire(build_wire_copy(rows, route), route)``."""
    return sanitize_wire(build_wire_copy(rows, route), route, heals=heals, memo=memo)


def finish_request(
    request: ChatRequest,
    *,
    markers: bool,
    ttl_setting: object = None,
    source: str = "",
    native: bool = False,
    static_prefix: str | None = None,
) -> ChatRequest:
    """The request with this route's cache plan applied (``markers``), or with every cache marker
    removed. ``native``: the route speaks the Anthropic Messages wire. Never mutates ``request``."""
    tools = cache_plan.strip_tool_markers(request.tools)
    if not markers:
        messages = cache_plan.strip_markers(request.messages)
    else:
        messages = cache_plan.plan_messages(
            request.messages,
            ttl=cache_plan.resolve_ttl(ttl_setting, source),
            native=native,
            static_prefix=static_prefix,
        )
    return replace(request, messages=messages, tools=tools)
