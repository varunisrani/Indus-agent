"""P09-16: the review prompts are pinned byte for byte (``--update-golden`` rewrites the file)."""

from __future__ import annotations

from pathlib import Path

from indus.learning.review.prompts import golden_text

GOLDEN = Path(__file__).resolve().parent / "review_prompts.txt"


def test_golden_review_prompts(update_golden: bool) -> None:
    current = golden_text()
    if update_golden:
        GOLDEN.write_text(current, encoding="utf-8")
    assert GOLDEN.read_text(encoding="utf-8") == current
