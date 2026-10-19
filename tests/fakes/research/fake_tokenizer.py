"""A whitespace tokenizer standing in for ``transformers`` (P22-11).

``FakeTokenizer.encode(text)`` returns one id per whitespace-separated word; a text containing
``REFUSE_MARK`` raises, to exercise the length // 4 fallback. ``RecordingLoader`` has the shape of
``AutoTokenizer.from_pretrained`` and keeps every call's arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REFUSE_MARK = "\x00refuse\x00"


class FakeTokenizer:
    def encode(self, text: str) -> list[int]:
        if REFUSE_MARK in text:
            raise ValueError("cannot encode this text")
        return [hash(word) & 0xFFFF for word in text.split()]


@dataclass
class RecordingLoader:
    fail: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def __call__(self, name: str, **options: Any) -> FakeTokenizer:
        self.calls.append((name, dict(options)))
        if self.fail is not None:
            raise self.fail
        return FakeTokenizer()


def words(count: int, word: str = "w") -> str:
    """A text of ``count`` tokens for the fake tokenizer."""
    return " ".join([word] * count)
