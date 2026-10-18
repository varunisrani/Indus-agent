"""Usage-anchored token accounting (P08-01, R01.8)."""

from __future__ import annotations

import asyncio
from typing import Any

from indus.core.compression.anchor import (
    ANCHOR_KEY,
    DEFAULT_IMAGE_COST,
    ImageCosts,
    MemoryBlobStore,
    SessionBlobStore,
    TokenMeter,
    UsageAnchor,
    anchored_tokens,
    capture,
    estimate_message,
    estimate_messages,
    estimate_text,
)
from indus.foundation.scope import Scope
from indus.state import registry, sessions


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def tool(text: str, ident: str = "c1") -> dict[str, Any]:
    return {"role": "tool", "content": text, "tool_call_id": ident}


PRICED = [user("please look at the repository"), assistant("calling a tool"), tool("x" * 400)]


def test_anchor_plus_appended_rows() -> None:
    anchor = capture(1_000, 50, PRICED)
    assert anchor is not None
    reply = assistant("the reply the anchored response produced")
    appended = [user("one more thing"), assistant("ok"), user("and another")]
    messages = [*PRICED, reply, *appended]
    assert anchored_tokens(messages, anchor) == 1_050 + estimate_messages(appended)
    # without the reply row the appended rows are all counted
    assert anchored_tokens([*PRICED, *appended], anchor) == 1_050 + estimate_messages(appended)
    assert anchored_tokens(PRICED, anchor) == 1_050


def test_editing_a_priced_row_drops_the_anchor() -> None:
    anchor = capture(1_000, 50, PRICED)
    assert anchor is not None
    edited = [user("please look at the repository!"), *PRICED[1:]]
    assert anchored_tokens(edited, anchor) is None
    shortened = PRICED[:2]
    assert anchored_tokens(shortened, anchor) is None
    # a different earlier row with the same last row still breaks the prefix fingerprint
    swapped = [user("something else"), *PRICED[1:]]
    assert anchored_tokens(swapped, anchor) is None


def test_unusable_usage_takes_no_anchor() -> None:
    assert capture(0, 10, PRICED) is None
    assert capture(None, 10, PRICED) is None
    assert capture(100, 10, []) is None
    assert UsageAnchor.from_json({"prompt_tokens": 5}) is None
    assert UsageAnchor.from_json("nope") is None


def test_estimates_follow_what_is_sent() -> None:
    assert estimate_text("") == 0
    assert estimate_text("abcd") == 1
    assert estimate_text("abcde") == 2
    sidecar = {"role": "user", "content": "x" * 4_000, "api_content": "short"}
    assert estimate_message(sidecar) < 100
    blob = {
        "role": "assistant",
        "content": "hi",
        "reasoning_details": [{"type": "reasoning.encrypted", "encrypted_content": "Z" * 50_000}],
    }
    assert estimate_message(blob) < 100
    image = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 90_000}},
        ],
    }
    assert DEFAULT_IMAGE_COST <= estimate_message(image) < DEFAULT_IMAGE_COST + 100
    assert estimate_message(image, image_cost=10) < 120


def test_no_anchor_and_a_huge_estimate_waits() -> None:
    meter = TokenMeter()
    huge = [user("x" * 4_000_000)]
    measure = meter.measure(huge)
    assert not measure.anchored and measure.tokens >= 1_000_000
    assert meter.verdict(measure, 150_000) == "wait"
    assert meter.verdict(meter.measure([user("small")]), 150_000) == "ok"


def test_usage_less_provider_lets_the_estimate_decide() -> None:
    meter = TokenMeter()
    huge = [user("x" * 1_000_000)]
    assert asyncio.run(meter.observe(None, None, huge)) is False
    assert meter.verdict(meter.measure(huge), 150_000) == "compress"


def test_real_reading_over_the_line_lets_the_estimate_decide() -> None:
    meter = TokenMeter()
    asyncio.run(meter.observe(160_000, 10, PRICED))
    edited = [user("changed"), *PRICED[1:], user("x" * 800_000)]
    measure = meter.measure(edited)
    assert not measure.anchored
    assert meter.verdict(measure, 150_000) == "compress"


def test_anchored_figure_over_the_line_compresses() -> None:
    meter = TokenMeter()
    asyncio.run(meter.observe(149_000, 500, PRICED))
    grown = [*PRICED, assistant("reply"), user("y" * 8_000)]
    measure = meter.measure(grown)
    assert measure.anchored
    assert meter.verdict(measure, 150_000) == "compress"
    assert meter.verdict(meter.measure(PRICED), 150_000) == "ok"


def test_after_a_compaction_the_estimate_waits_for_real_usage() -> None:
    meter = TokenMeter()
    asyncio.run(meter.observe(200_000, 10, PRICED))
    asyncio.run(meter.clear(after_compaction=True))
    assert meter.anchor is None
    big = [user("x" * 800_000)]
    assert meter.verdict(meter.measure(big), 150_000) == "wait"
    asyncio.run(meter.observe(90_000, 10, big))
    assert meter.awaiting_real is False


def test_image_cost_learned_from_the_residual() -> None:
    images = ImageCosts()
    meter = TokenMeter(images=images, image_key=ImageCosts.key("vision-1", "api.example.test"))
    asyncio.run(meter.observe(1_000, 0, PRICED))
    picture = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        ],
    }
    priced = [*PRICED, picture]
    text_only = anchored_tokens(priced, meter.anchor, 0)
    assert text_only is not None
    asyncio.run(meter.observe(text_only + 2 * 800, 0, priced))
    assert images.cost("vision-1@api.example.test") == 800
    # an implausible residual teaches nothing
    assert images.learn("k", 10) is None and images.learn("k", 50_000) is None
    assert images.cost("k") == DEFAULT_IMAGE_COST


def test_memory_store_restore_and_clear() -> None:
    blobs = MemoryBlobStore()
    first = TokenMeter(session_id="s1", store=blobs)
    asyncio.run(first.observe(5_000, 100, PRICED))
    assert blobs.blobs[("s1", ANCHOR_KEY)]
    second = TokenMeter(session_id="s1", store=blobs)
    assert asyncio.run(second.restore(PRICED)) is True
    assert second.anchor == first.anchor
    asyncio.run(second.clear())
    assert ("s1", ANCHOR_KEY) not in blobs.blobs


def test_restart_reload_keeps_the_anchor(scope: Scope) -> None:
    handle = registry.open_unshared(scope)
    try:
        sessions.create(handle, "s1", "cli")
        first = TokenMeter(session_id="s1", store=SessionBlobStore(handle))
        asyncio.run(first.observe(12_345, 67, PRICED))
        row = sessions.get(handle, "s1")
        assert row is not None
        stored = sessions.parse_model_config(row["model_config"])[ANCHOR_KEY]
        assert stored["prompt_tokens"] == 12_345
        restarted = TokenMeter(session_id="s1", store=SessionBlobStore(handle))
        assert asyncio.run(restarted.restore([*PRICED, assistant("r")])) is True
        assert restarted.anchor == first.anchor
        assert restarted.measure(PRICED).tokens == 12_345 + 67
    finally:
        handle.close()


def test_stale_persisted_anchor_is_cleared(scope: Scope) -> None:
    handle = registry.open_unshared(scope)
    try:
        sessions.create(handle, "s1", "cli")
        blobs = SessionBlobStore(handle)
        asyncio.run(TokenMeter(session_id="s1", store=blobs).observe(9_000, 1, PRICED))
        restarted = TokenMeter(session_id="s1", store=blobs)
        assert asyncio.run(restarted.restore([user("a different history")])) is False
        assert restarted.anchor is None
        assert asyncio.run(blobs.load("s1", ANCHOR_KEY)) is None
    finally:
        handle.close()
