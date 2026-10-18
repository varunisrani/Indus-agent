"""Token accounting anchored on the provider's own count (P08-01, R01.8).

The provider's last real ``prompt_tokens`` (+ ``completion_tokens``) is the only trusted size of a
conversation. A ``UsageAnchor`` records that count together with *which* messages it priced: how
many there were, a fingerprint of the last one and a fingerprint of the whole priced prefix. The
size of the conversation later is ``anchor + estimate(messages appended since)``; the reply that
the anchored response produced sits at index ``base_count`` and is already in
``completion_tokens``, so the walk skips it. Any change to the priced prefix (an edit, a rewind,
a compaction, a reload that differs) breaks the fingerprints and the anchor is ignored.

Without a usable anchor only a rough estimate of the whole transcript exists. Estimates are not
evidence: ``TokenMeter.verdict`` answers ``wait`` for an over-threshold estimate (send one request
and let the provider count) unless the provider has shown it never reports usage or its last
real count was already over the line.

Estimates: 4 characters per token over what the provider is sent (``api_content`` replaces
``content``; opaque encrypted reasoning blobs count 0); every image costs a per-``model@host``
amount learned from the residual between a real count and the text-only projection (1,500 before
the first vision turn).

The anchor is persisted in the session row's ``model_config`` under ``usage_anchor`` so a restarted
process keeps it while the stored transcript still matches; a compaction or reset clears it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

if TYPE_CHECKING:
    from indus.state.registry import StoreHandle

log = logging.getLogger(__name__)

__all__ = [
    "ANCHOR_KEY",
    "CHARS_PER_TOKEN",
    "DEFAULT_IMAGE_COST",
    "ConfigBlobStore",
    "ImageCosts",
    "Measure",
    "MemoryBlobStore",
    "SessionBlobStore",
    "TokenMeter",
    "UsageAnchor",
    "Verdict",
    "anchored_tokens",
    "capture",
    "count_images",
    "estimate_message",
    "estimate_messages",
    "estimate_text",
    "fingerprint",
    "matches",
    "prefix_fingerprint",
]

ANCHOR_KEY = "usage_anchor"
CHARS_PER_TOKEN = 4
DEFAULT_IMAGE_COST = 1_500
IMAGE_COST_BAND = (64, 32_768)
IMAGE_COST_SMOOTHING = 0.5

IMAGE_PART_TYPES = frozenset({"image", "image_url", "input_image"})
# Keys whose values are ciphertext or signatures the provider prices by its own count.
OPAQUE_KEYS = frozenset({"encrypted_content", "signature", "data", "redacted_thinking"})
# The provider-visible fields that survive a store round trip byte for byte.
FINGERPRINT_FIELDS = ("role", "content", "api_content", "tool_call_id", "tool_calls")

Verdict = Literal["ok", "wait", "compress"]


# -- estimates ----------------------------------------------------------------------------------


def estimate_text(text: str) -> int:
    """Rough tokens for ``text``: characters / 4, rounded up (never 0 for non-empty text)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def _blank_opaque(value: object) -> object:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, inner in cast(Mapping[str, Any], value).items():
            out[key] = "" if key in OPAQUE_KEYS else _blank_opaque(inner)
        return out
    if isinstance(value, list):
        return [_blank_opaque(v) for v in cast(list[Any], value)]
    return value


def _is_image(part: object) -> bool:
    return (
        isinstance(part, Mapping) and cast(Mapping[str, Any], part).get("type") in IMAGE_PART_TYPES
    )


def _wire_shadow(msg: Mapping[str, Any]) -> dict[str, Any]:
    """What the provider receives of ``msg``, with image payloads and opaque blobs removed."""
    shadow: dict[str, Any] = {}
    sidecar = msg.get("api_content")
    sidecar_wins = (
        isinstance(sidecar, str)
        and bool(sidecar)
        and msg.get("role")
        in (
            "user",
            "assistant",
        )
    )
    for key, value in msg.items():
        if key == "api_content":
            if sidecar_wins:
                shadow["content"] = value
            continue
        if key == "content":
            if sidecar_wins:
                continue
            if isinstance(value, list):
                shadow["content"] = [
                    {"type": "image"} if _is_image(p) else p for p in cast(list[Any], value)
                ]
                continue
        if key == "reasoning_details":
            shadow[key] = _blank_opaque(value)
            continue
        if key in OPAQUE_KEYS:
            continue
        shadow[key] = value
    return shadow


def count_images(msg: Mapping[str, Any]) -> int:
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    return sum(1 for part in cast(list[Any], content) if _is_image(part))


def _text_tokens(msg: Mapping[str, Any]) -> int:
    try:
        raw = json.dumps(_wire_shadow(msg), ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        raw = repr(msg)
    return estimate_text(raw)


def estimate_message(msg: Mapping[str, Any], image_cost: int = DEFAULT_IMAGE_COST) -> int:
    return _text_tokens(msg) + count_images(msg) * max(0, image_cost)


def estimate_messages(
    messages: Sequence[Mapping[str, Any]], image_cost: int = DEFAULT_IMAGE_COST
) -> int:
    return sum(estimate_message(m, image_cost) for m in messages)


# -- fingerprints -------------------------------------------------------------------------------


def fingerprint(msg: Mapping[str, Any]) -> str:
    """A digest of ``msg`` over its provider-visible, persisted fields."""
    payload = {k: msg.get(k) for k in FINGERPRINT_FIELDS if msg.get(k) is not None}
    try:
        raw = json.dumps(
            payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        raw = repr(sorted(payload.items()))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def prefix_fingerprint(messages: Sequence[Mapping[str, Any]], count: int) -> str | None:
    """A digest of the first ``count`` messages (roles + fingerprints); None when too short."""
    if count <= 0 or len(messages) < count:
        return None
    parts = [(m.get("role"), fingerprint(m)) for m in messages[:count]]
    raw = json.dumps(parts, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


# -- the anchor ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageAnchor:
    prompt_tokens: int
    completion_tokens: int
    base_count: int
    last_role: str | None
    last_fp: str
    prefix_fp: str

    def to_json(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "base_count": self.base_count,
            "last_role": self.last_role,
            "last_fp": self.last_fp,
            "prefix_fp": self.prefix_fp,
        }

    @classmethod
    def from_json(cls, data: object) -> UsageAnchor | None:
        """The anchor in ``data``, or None for anything malformed (fails closed)."""
        if not isinstance(data, Mapping):
            return None
        blob = cast(Mapping[str, Any], data)
        try:
            prompt = int(blob.get("prompt_tokens") or 0)
            completion = int(blob.get("completion_tokens") or 0)
            count = int(blob.get("base_count") or 0)
        except (TypeError, ValueError):
            return None
        last_fp, prefix_fp, role = blob.get("last_fp"), blob.get("prefix_fp"), blob.get("last_role")
        if prompt <= 0 or count <= 0 or not isinstance(last_fp, str) or not last_fp:
            return None
        if not isinstance(prefix_fp, str) or not prefix_fp:
            return None
        return cls(
            prompt_tokens=prompt,
            completion_tokens=max(0, completion),
            base_count=count,
            last_role=role if isinstance(role, str) else None,
            last_fp=last_fp,
            prefix_fp=prefix_fp,
        )


def capture(
    prompt_tokens: object, completion_tokens: object, messages: Sequence[Mapping[str, Any]]
) -> UsageAnchor | None:
    """An anchor for the response that priced ``messages``; None when usage is unusable."""
    try:
        prompt = int(prompt_tokens or 0)  # pyright: ignore[reportArgumentType]
        completion = int(completion_tokens or 0)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return None
    if prompt <= 0 or not messages:
        return None
    prefix = prefix_fingerprint(messages, len(messages))
    if prefix is None:
        return None
    last = messages[-1]
    role = last.get("role")
    return UsageAnchor(
        prompt_tokens=prompt,
        completion_tokens=max(0, completion),
        base_count=len(messages),
        last_role=role if isinstance(role, str) else None,
        last_fp=fingerprint(last),
        prefix_fp=prefix,
    )


def matches(messages: Sequence[Mapping[str, Any]], anchor: UsageAnchor) -> bool:
    """Do ``messages`` still begin with exactly the prefix ``anchor`` priced?"""
    count = anchor.base_count
    if count <= 0 or len(messages) < count:
        return False
    last = messages[count - 1]
    if last.get("role") != anchor.last_role or fingerprint(last) != anchor.last_fp:
        return False
    return prefix_fingerprint(messages, count) == anchor.prefix_fp


def _delta(
    messages: Sequence[Mapping[str, Any]], anchor: UsageAnchor
) -> Sequence[Mapping[str, Any]]:
    delta = messages[anchor.base_count :]
    if delta and delta[0].get("role") == "assistant":
        delta = delta[1:]  # the anchored reply, already in completion_tokens
    return delta


def anchored_tokens(
    messages: Sequence[Mapping[str, Any]],
    anchor: UsageAnchor | None,
    image_cost: int = DEFAULT_IMAGE_COST,
) -> int | None:
    """Anchor total + estimate of the messages appended since; None when stale or missing."""
    if anchor is None or not matches(messages, anchor):
        return None
    total = anchor.prompt_tokens + anchor.completion_tokens
    return total + estimate_messages(_delta(messages, anchor), image_cost)


# -- learned image costs ------------------------------------------------------------------------


@dataclass
class ImageCosts:
    """Per-``model@host`` image cost learned from provider usage (smoothed, plausibility-banded)."""

    table: dict[str, int] = field(default_factory=dict[str, int])

    @staticmethod
    def key(model: str, host: str) -> str:
        return f"{model}@{host}"

    def cost(self, key: str) -> int:
        return self.table.get(key, DEFAULT_IMAGE_COST)

    def learn(self, key: str, per_image: int) -> int | None:
        low, high = IMAGE_COST_BAND
        if not low <= per_image <= high:
            return None
        prior = self.table.get(key)
        learned = (
            per_image if prior is None else int(prior + IMAGE_COST_SMOOTHING * (per_image - prior))
        )
        self.table[key] = learned
        return learned


# -- persistence --------------------------------------------------------------------------------


class ConfigBlobStore(Protocol):
    """Small JSON blobs kept on the session row (``model_config`` keys)."""

    async def load(self, session_id: str, key: str) -> object: ...

    async def save(self, session_id: str, key: str, value: object) -> None: ...


class MemoryBlobStore:
    """In-process blobs (unsaved sessions and tests)."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], object] = {}

    async def load(self, session_id: str, key: str) -> object:
        return self.blobs.get((session_id, key))

    async def save(self, session_id: str, key: str, value: object) -> None:
        if value is None:
            self.blobs.pop((session_id, key), None)
        else:
            self.blobs[(session_id, key)] = value


class SessionBlobStore:
    """Blobs in the P02 ``sessions.model_config`` column (``None`` deletes the key)."""

    def __init__(self, handle: StoreHandle) -> None:
        self.handle = handle

    async def load(self, session_id: str, key: str) -> object:
        from indus.foundation.blocking import run_blocking
        from indus.state import sessions

        def read() -> object:
            row = sessions.get(self.handle, session_id)
            if row is None:
                return None
            return sessions.parse_model_config(row.get("model_config")).get(key)

        return await run_blocking(read, pool="io", deadline=30.0, label="read session config")

    async def save(self, session_id: str, key: str, value: object) -> None:
        from indus.state import sessions

        patch = {key: value}
        await self.handle.awrite(
            lambda conn: sessions.patch_model_config_in(conn, session_id, patch)
        )


# -- the meter ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Measure:
    tokens: int
    anchored: bool


@dataclass
class TokenMeter:
    """One session's view of its context size (see the module notes)."""

    session_id: str = ""
    store: ConfigBlobStore | None = None
    images: ImageCosts = field(default_factory=ImageCosts)
    image_key: str = ""
    anchor: UsageAnchor | None = None
    last_real_prompt: int = 0
    last_real_completion: int = 0
    usage_less: bool = False
    awaiting_real: bool = False

    @property
    def image_cost(self) -> int:
        return self.images.cost(self.image_key)

    def measure(self, messages: Sequence[Mapping[str, Any]]) -> Measure:
        anchored = anchored_tokens(messages, self.anchor, self.image_cost)
        if anchored is not None:
            return Measure(anchored, True)
        return Measure(estimate_messages(messages, self.image_cost), False)

    def verdict(self, measure: Measure, threshold: int) -> Verdict:
        """``ok`` below ``threshold``; an anchored figure over it → ``compress``; a bare estimate
        over it → ``wait`` unless real evidence already says the session is over (R01.8)."""
        if threshold <= 0 or measure.tokens < threshold:
            return "ok"
        if measure.anchored:
            return "compress"
        if self.awaiting_real:
            return "wait"
        if self.last_real_prompt >= threshold:
            return "compress"
        return "compress" if self.usage_less else "wait"

    async def observe(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        messages: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Fold one response's usage in; ``messages`` are the ones its request priced. True when
        the usage was real (a new anchor was taken)."""
        if not prompt_tokens or prompt_tokens <= 0:
            self.usage_less = True
            return False
        self._learn_image_cost(prompt_tokens, messages)
        self.usage_less = False
        self.awaiting_real = False
        self.last_real_prompt = prompt_tokens
        self.last_real_completion = max(0, completion_tokens or 0)
        anchor = capture(prompt_tokens, completion_tokens, messages)
        if anchor is not None:
            await self._set(anchor)
        return True

    def _learn_image_cost(self, real: int, messages: Sequence[Mapping[str, Any]]) -> None:
        anchor = self.anchor
        if anchor is None or not self.image_key:
            return
        delta = _delta(messages, anchor) if matches(messages, anchor) else ()
        images = sum(count_images(m) for m in delta)
        if images <= 0:
            return
        text_only = anchored_tokens(messages, anchor, 0)
        if text_only is None:
            return
        learned = self.images.learn(self.image_key, (real - text_only) // images)
        if learned is not None:
            log.info("image cost for %s learned from usage: %d tokens", self.image_key, learned)

    async def clear(self, *, after_compaction: bool = False) -> None:
        """Forget the anchor (compaction, reset). After a compaction the next estimate waits for
        one real reading."""
        self.awaiting_real = after_compaction
        await self._set(None)

    async def restore(self, messages: Sequence[Mapping[str, Any]]) -> bool:
        """Adopt the persisted anchor while ``messages`` still carry its prefix; else clear it."""
        if self.anchor is not None or self.store is None or not self.session_id:
            return self.anchor is not None
        try:
            stored = UsageAnchor.from_json(await self.store.load(self.session_id, ANCHOR_KEY))
        except Exception:
            log.warning("could not read the usage anchor of %s", self.session_id, exc_info=True)
            return False
        if stored is None:
            return False
        if matches(messages, stored):
            self.anchor = stored
            return True
        await self._set(None)
        return False

    async def _set(self, anchor: UsageAnchor | None) -> None:
        self.anchor = anchor
        if self.store is None or not self.session_id:
            return
        try:
            await self.store.save(
                self.session_id, ANCHOR_KEY, None if anchor is None else anchor.to_json()
            )
        except Exception:
            log.warning("could not store the usage anchor of %s", self.session_id, exc_info=True)
