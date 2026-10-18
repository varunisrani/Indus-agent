"""Record a live provider exchange as a replayable capture (developer only; never run in CI).

Usage::

    uv run python scripts/dev/record_capture.py --provider openrouter --model <id> --scenario text

The route (base URL, key) is resolved for the active profile the same way the runtime does it, the
scenario's request is sent through our own transport, and the raw response chunks are saved to
``tests/fixtures/captures/<api_mode>/<scenario>.json`` in the format described in
``tests/fakes/model/replay.py``.

Nothing secret may reach the file: request and response headers are kept only from small
allow-lists, the redactor runs over the whole file text, and the save is refused when any secret
the profile knows (or the route's key) still appears in the text or in the decoded response bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from indus import __version__
from indus.foundation.redact import redact_text

ROOT = Path(__file__).resolve().parents[2]
CAPTURES_DIR = ROOT / "tests" / "fixtures" / "captures"
REQUEST_HEADER_ALLOW = ("content-type", "anthropic-version")
RESPONSE_HEADER_ALLOW = ("content-type",)
DOC_URLS = {
    "chat_completions": "https://github.com/openai/openai-openapi (openapi.yaml)",
    "anthropic_messages": "https://platform.claude.com/docs/en/build-with-claude/streaming",
}
_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for one city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}
SCENARIOS: dict[str, dict[str, Any]] = {
    "text": {
        "messages": [{"role": "user", "content": "Answer with exactly one word: hello"}],
        "max_tokens": 64,
    },
    "tool_call": {
        "messages": [
            {"role": "user", "content": "Use the get_weather tool to look up the weather in Oslo."}
        ],
        "tools": [_WEATHER_TOOL],
        "max_tokens": 256,
    },
}


class CaptureRefused(Exception):
    """The capture would contain a secret; nothing was written."""


def _allowed(headers: Mapping[str, str], allow: Iterable[str]) -> dict[str, str]:
    keep = {a.lower() for a in allow}
    return {k.lower(): v for k, v in headers.items() if k.lower() in keep}


def build_capture(
    *,
    meta: Mapping[str, Any],
    request: httpx.Request,
    status: int,
    headers: Mapping[str, str],
    chunks: Sequence[bytes],
) -> dict[str, Any]:
    """The capture document; credentials and ids never pass the header allow-lists."""
    url = request.url
    return {
        "meta": dict(meta),
        "request": {
            "method": request.method,
            "url": f"{url.scheme}://{url.host}{url.path}",
            "headers": _allowed(dict(request.headers), REQUEST_HEADER_ALLOW),
            "body": json.loads(request.content) if request.content else None,
        },
        "response": {
            "status": status,
            "headers": _allowed(headers, RESPONSE_HEADER_ALLOW),
            "chunks": [base64.b64encode(c).decode("ascii") for c in chunks],
        },
    }


def render_capture(capture: Mapping[str, Any], secrets: Iterable[str]) -> str:
    """Redact the capture text and refuse if any known secret survives anywhere in it."""
    values = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)
    text = redact_text(json.dumps(capture, indent=2, ensure_ascii=False) + "\n", values)
    decoded = b"".join(base64.b64decode(c) for c in capture["response"]["chunks"])
    decoded_text = decoded.decode("utf-8", errors="replace")
    for value in values:
        if value in text or value in decoded_text:
            raise CaptureRefused(
                "a secret from this profile appears in the exchange; nothing was saved"
            )
    return text


def secrets_for(scope: Any, route_key: str | None) -> list[str]:
    """Every value that must never be written: the route key, store values, credential-like env."""
    found: list[str] = [route_key] if route_key else []
    store = getattr(scope, "secrets", None)
    if store is not None:
        found.extend(store.known_values())
    for name, value in dict(getattr(scope, "env", {}) or {}).items():
        upper = name.upper()
        if any(mark in upper for mark in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and value:
            found.append(value)
    return found


async def exchange(
    client: httpx.AsyncClient, request: httpx.Request
) -> tuple[int, dict[str, str], list[bytes]]:
    """Send ``request`` and keep the response bytes exactly as they arrive."""
    response = await client.send(request, stream=True)
    try:
        chunks = [chunk async for chunk in response.aiter_raw()]
    finally:
        await response.aclose()
    return response.status_code, dict(response.headers), chunks


def scenario_request(scenario: str, model: str) -> dict[str, Any]:
    return {"model": model, "stream": True, **SCENARIOS[scenario]}


async def _record(args: argparse.Namespace, scope: Any) -> Path:  # pragma: no cover - real key
    from indus.foundation.fsio import atomic_write_text
    from indus.providers.http import client_for, close_all, timeouts_for
    from indus.providers.route import resolve
    from indus.providers.transports import get_transport, secret_text
    from tests.fakes.model.replay import chat_request_from

    route = resolve(scope, model=args.model, provider=args.provider)
    transport = get_transport(route.api_mode)
    spec = scenario_request(args.scenario, args.model)
    meta = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": args.provider,
        "model": args.model,
        "api_mode": route.api_mode,
        "scenario": args.scenario,
        "base_url": route.base_url,
        "doc_url": DOC_URLS.get(route.api_mode, ""),
        "doc_date": datetime.now(UTC).date().isoformat(),
        "indus_version": __version__,
        "synthetic": False,
        "chat_request": spec,
    }
    req = chat_request_from({"meta": meta})
    request = transport.build_request(route, req)
    client = client_for(route, timeouts=timeouts_for(route, scope))
    try:
        status, headers, chunks = await exchange(client, request)
    finally:
        await close_all()  # the pooled client belongs to this one-shot run
    capture = build_capture(
        meta=meta, request=request, status=status, headers=headers, chunks=chunks
    )
    text = render_capture(capture, secrets_for(scope, secret_text(route.api_key)))
    out = CAPTURES_DIR / route.api_mode / f"{args.scenario}.json"
    atomic_write_text(out, text)
    return out


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - developer entry point
    parser = argparse.ArgumentParser(description=(__doc__ or "record a capture").splitlines()[0])
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    from indus.foundation.bootstrap import run as bootstrap
    from indus.foundation.scope import use_scope

    scope, rest = bootstrap(list(sys.argv[1:] if argv is None else argv))
    args = parser.parse_args(rest)
    try:
        with use_scope(scope):
            path = asyncio.run(_record(args, scope))
    except CaptureRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"saved {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
