"""Small helpers shared by the provider tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Coroutine, Iterable
from typing import Any

from indus.providers.profile import ProviderProfile
from indus.providers.route import SOURCE_EXPLICIT, RuntimeRoute, Secret
from indus.providers.sse import SSEDecoder, SSEItem
from indus.providers.transports import Transport
from indus.providers.types import StreamEvent

FAKE_KEY = "test-key-" + "0000-" + "abcd"


def make_profile(**fields: Any) -> ProviderProfile:
    """A valid ``ProviderProfile`` for transport tests; ``fields`` override the defaults."""
    fields.setdefault("name", "test")
    fields.setdefault("base_url", "https://api.fake.invalid/v1")
    return ProviderProfile(**fields)


def make_route(
    api_mode: str = "chat_completions",
    *,
    base_url: str | None = None,
    model: str = "test-model",
    key: str | None = FAKE_KEY,
    profile: ProviderProfile | None = None,
    provider: str | None = None,
) -> RuntimeRoute:
    """A resolved route for transport tests (the real ``RuntimeRoute``, built directly)."""
    if base_url is None:
        base_url = (
            "https://api.fake.invalid/v1"
            if api_mode == "chat_completions"
            else "https://anthropic.fake.invalid"
        )
    return RuntimeRoute(
        provider=provider or (profile.name if profile is not None else "test"),
        model=model,
        api_mode=api_mode,
        base_url=base_url,
        api_key=Secret(key) if key else None,
        source=SOURCE_EXPLICIT,
        profile=profile,
    )


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _items(chunks: Iterable[bytes]) -> AsyncIterator[SSEItem]:
    decoder = SSEDecoder()
    for chunk in chunks:
        for item in decoder.feed(chunk):
            yield item
    for item in decoder.flush():
        yield item


def parse(transport: Transport, chunks: Iterable[bytes]) -> list[StreamEvent]:
    async def main() -> list[StreamEvent]:
        return [e async for e in transport.parse_stream(_items(chunks))]

    return run(main())


def sse(*frames: dict[str, Any] | str, event: str | None = None) -> bytes:
    """Encode frames as SSE: dicts become JSON data lines; strings are sent as-is as data."""
    out = []
    for frame in frames:
        data = frame if isinstance(frame, str) else json.dumps(frame)
        head = f"event: {event}\n" if event else ""
        out.append(f"{head}data: {data}\n\n")
    return "".join(out).encode()


def anthropic_sse(*frames: dict[str, Any]) -> bytes:
    return b"".join(f"event: {f['type']}\ndata: {json.dumps(f)}\n\n".encode() for f in frames)


def body_of(request: Any) -> dict[str, Any]:
    return json.loads(request.content)
