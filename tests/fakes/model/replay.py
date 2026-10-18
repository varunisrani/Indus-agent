"""Replay a recorded capture through ``httpx.MockTransport``.

A capture (``tests/fixtures/captures/<api_mode>/<scenario>.json``) holds one exchange: the request
our transport sent and the response bytes exactly as they arrived, chunk by chunk (base64). The
replay transport checks that the outgoing request matches the recording (method, host, path and
JSON body, ignoring a few volatile fields) and then serves the same chunks in the same order.

Format::

    {"meta": {"recorded_at", "provider", "model", "api_mode", "scenario", "base_url",
              "doc_url", "doc_date", "indus_version", "synthetic", "chat_request"},
     "request": {"method", "url", "headers", "body"},
     "response": {"status", "headers", "chunks": [<base64>, ...]}}

``meta.chat_request`` is the provider-neutral request (model, messages, tools, max_tokens, ...)
so a test can rebuild the ``ChatRequest`` that must produce ``request.body``.
"""

from __future__ import annotations

import base64
import difflib
import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import httpx

from indus.providers.types import ChatRequest

CAPTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "captures"
VOLATILE_BODY_FIELDS: frozenset[str] = frozenset({"metadata", "user"})


class ReplayMismatch(AssertionError):
    """The outgoing request differs from the recorded one; the message holds a diff."""


def load_capture(path_or_name: str | Path) -> dict[str, Any]:
    """Load a capture by path or by ``"<api_mode>/<scenario>"``."""
    path = Path(path_or_name)
    if not path.suffix:
        path = CAPTURES_DIR / f"{path_or_name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def all_captures() -> list[Path]:
    return sorted(CAPTURES_DIR.glob("*/*.json"))


def capture_chunks(capture: dict[str, Any]) -> list[bytes]:
    return [base64.b64decode(c) for c in capture["response"]["chunks"]]


def chat_request_from(capture: dict[str, Any]) -> ChatRequest:
    spec = dict(capture["meta"]["chat_request"])
    return ChatRequest(
        model=spec["model"],
        messages=list(spec.get("messages", [])),
        tools=list(spec.get("tools", [])),
        max_tokens=spec.get("max_tokens"),
        temperature=spec.get("temperature"),
        reasoning_effort=spec.get("reasoning_effort"),
        stream=bool(spec.get("stream", True)),
        extra_body=dict(spec.get("extra_body", {})),
        tool_choice=spec.get("tool_choice"),
    )


def _comparable(body: Any, ignore: Iterable[str]) -> Any:
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if k not in set(ignore)}
    return body


def _pretty(value: Any) -> list[str]:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).splitlines(keepends=True)


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class ReplayTransport(httpx.MockTransport):
    """Serve ``capture`` for every matching request (``requests`` records what was sent)."""

    def __init__(
        self, capture: dict[str, Any], *, ignore: Iterable[str] = VOLATILE_BODY_FIELDS
    ) -> None:
        self.capture = capture
        self.ignore = frozenset(ignore)
        self.requests: list[httpx.Request] = []
        self.streams: list[_Chunks] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        recorded = self.capture["request"]
        want_url = httpx.URL(recorded["url"])
        problems: list[str] = []
        if request.method != recorded["method"]:
            problems.append(f"method {request.method} != recorded {recorded['method']}")
        if (request.url.host, request.url.path) != (want_url.host, want_url.path):
            problems.append(f"url {request.url} != recorded {want_url}")
        sent = json.loads(request.content) if request.content else None
        want = _comparable(recorded["body"], self.ignore)
        got = _comparable(sent, self.ignore)
        if got != want:
            diff = "".join(
                difflib.unified_diff(_pretty(want), _pretty(got), "recorded body", "sent body")
            )
            problems.append("request body differs from the recording:\n" + diff)
        if problems:
            raise ReplayMismatch("\n".join(problems))
        response = self.capture["response"]
        stream = _Chunks(capture_chunks(self.capture))
        self.streams.append(stream)
        return httpx.Response(
            int(response["status"]), headers=dict(response.get("headers", {})), stream=stream
        )
