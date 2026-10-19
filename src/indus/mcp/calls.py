"""Calling an MCP tool and turning its answer into a tool result (P13-06).

One call: trust gate (P13-07, before any transport work, a lazy start included) → breaker →
the server's call slot (one call at a time unless ``supports_parallel_tool_calls``) → a live
session (lazy start, recycled restart, respawn wait: ``ServerHandle.session``) → ``tools/call``
with the server's own tool name, bounded by the server's ``timeout`` on the clock, inside
``requesters.serving(...)`` when the target has P13-10's requester slot (a sampling or elicitation
request the server sends during the call reaches this call's session) → classify → breaker
bookkeeping → render.

Failure classes (``error_type`` of the result): ``auth`` (HTTP 401/403), ``transport`` (no
session, connection lost, no answer in time), ``not_found`` (JSON-RPC −32601), ``tool_error``
(``isError``, or another JSON-RPC error the server answered with). Every class is a breaker strike;
a success clears the streak. Nothing is replayed automatically.

The result is JSON text ``{"result": text, "structuredContent"?: …, "_meta"?: …}``:

- text parts joined by newlines, Unicode TAG characters stripped and every ``MEDIA:`` directive
  in the server's own text neutralised (``neutral``: a server must not be able to name a local
  file for delivery to the user); images saved under ``<home>/cache/images/`` and audio under
  ``<home>/cache/audio/``, each named by a ``MEDIA:<path>`` line (the only ``MEDIA:`` lines a
  result carries are these, of files this call wrote); embedded resources inline (text) or saved
  under ``<home>/cache/documents/`` (blobs, at most 50 MiB, checked before decoding); resource
  links point at the server's ``read_resource`` tool; any other block leaves a "dropped" notice;
- ``structuredContent`` kept unless a text block carries the same JSON (the spec's dual emit), and
  used as ``result`` when the blocks rendered nothing;
- ``_meta`` without protocol-reserved keys (a ``modelcontextprotocol`` or ``mcp`` label followed by
  another label before the ``/``);
- text and serialised ``structuredContent`` each capped at 2,000,000 characters (head 40 % / tail
  60 %); the P04 spill (50,000 for ``mcp__`` tools) and the untrusted fence come after.

With ``native_vision`` (the main model reads images in tool results) up to 4 of the call's images,
from at most 16 candidates, are fitted to the P10 embed target and attached as the multimodal
envelope; the others stay ``MEDIA:`` paths.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import mimetypes
import re
import secrets
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import anyio
import mcp_types as types
from mcp.shared.exceptions import MCPError

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_bytes
from indus.foundation.scope import Scope
from indus.mcp import trust
from indus.mcp.config import McpServer
from indus.mcp.connect import LiveSession, clock_timeout, first_cause
from indus.mcp.lifecycle import McpServerUnavailable
from indus.mcp.naming import strip_tags, tool_name
from indus.protocol.requests_port import ServerRequestPort
from indus.safety.approvals.decision import ApprovalState
from indus.tools.calls import ToolResult
from indus.tools.truncate import head_tail

__all__ = [
    "MAX_NATIVE_IMAGES",
    "MAX_RESOURCE_BYTES",
    "MAX_RESULT_CHARS",
    "NATIVE_IMAGE_CANDIDATES",
    "UTILITY_KINDS",
    "CallContext",
    "CallTarget",
    "Rendered",
    "ToolSpec",
    "call",
    "classify",
    "drop_reserved_meta",
    "is_reserved_meta_key",
    "neutral",
    "render_result",
]

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 2_000_000
MAX_RESOURCE_BYTES = 50 * 1024 * 1024
_MAX_RESOURCE_B64 = MAX_RESOURCE_BYTES * 4 // 3 + 4
MAX_NATIVE_IMAGES = 4
NATIVE_IMAGE_CANDIDATES = 16
LIST_MAX_PAGES = 50

ToolKind = Literal["tool", "list_resources", "read_resource", "list_prompts", "get_prompt"]
UTILITY_KINDS: tuple[ToolKind, ...] = (
    "list_resources",
    "read_resource",
    "list_prompts",
    "get_prompt",
)


@dataclass(frozen=True)
class ToolSpec:
    """One registered MCP tool: its registry name, the server's own name for it (for a utility,
    the utility kind), and the read-only hint frozen at the listing it came from."""

    registry_name: str
    server_tool: str
    kind: ToolKind = "tool"
    read_only: bool = False


class CallTarget(Protocol):
    """What a call needs from the server it goes to (``discovery.ServerHandle``)."""

    name: str
    config: McpServer
    breaker: trust.CircuitBreaker
    home: Path
    clock: Clock

    def slot(self) -> AbstractAsyncContextManager[None]:
        """Held for the whole call: one call at a time unless the server allows parallel ones."""
        ...

    def session(self) -> AbstractAsyncContextManager[LiveSession]:
        """A ready session for one request (starting a lazy server, waking a recycled one)."""
        ...


@dataclass(frozen=True)
class CallContext:
    """Who the call runs for (from the dispatch keywords)."""

    scope: Scope | None = None
    session_id: str | None = None
    turn_id: str | None = None
    broker: ServerRequestPort | None = None
    run_context: object = None
    judge: Any = None
    native_vision: bool = False
    approvals: ApprovalState | None = None


# -- server text --------------------------------------------------------------------------------

# A ``MEDIA:`` directive in any letter case, with spaces or a full-width colon before the colon.
_MEDIA_DIRECTIVE = re.compile(r"(media)\s*[:\uff1a\ufe55]", re.IGNORECASE)


def neutral(text: str) -> str:
    """Server text as the model may see it: TAG characters stripped and every ``MEDIA:``
    directive defused, so a server cannot make a delivery scanner attach a local file of its
    choosing (``MEDIA:/home/me/.ssh/id_ed25519``). The path itself stays readable."""
    cleaned = strip_tags(text)
    if "media" not in cleaned.lower():
        return cleaned
    return _MEDIA_DIRECTIVE.sub(r"[\1 directive from the MCP server removed] ", cleaned)


def _neutral_tree(value: Any) -> Any:
    """``neutral`` over every string (keys included) of a JSON-like value."""
    if isinstance(value, str):
        return neutral(value)
    if isinstance(value, Mapping):
        return {
            (neutral(k) if isinstance(k, str) else k): _neutral_tree(v)
            for k, v in cast(Mapping[object, Any], value).items()
        }
    if isinstance(value, list | tuple):
        return [_neutral_tree(v) for v in cast(Sequence[Any], value)]
    return value


# -- errors -------------------------------------------------------------------------------------

_TRANSPORT_CODES = frozenset({types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT})
_TRANSPORT_TYPES: tuple[type[BaseException], ...] = (
    McpServerUnavailable,
    ConnectionError,
    OSError,
    EOFError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)


def _error_json(message: str, kind: str, server: str) -> str:
    # Error texts quote the server (its error message, its isError text): neutralised too.
    return cast(str, ToolResult.error(neutral(message), error_type=kind, server=server).content)


def classify(exc: BaseException, server: str, what: str, timeout: float) -> tuple[str, str]:
    """``(error_type, message)`` for a call that raised."""
    root = first_cause(exc)
    if trust.is_auth_failure(exc):
        return (
            "auth",
            f"MCP server {server} refused its credentials. The user has to sign in again "
            f"(indus mcp login {server}); do not retry this tool until then.",
        )
    if isinstance(root, TimeoutError) and not isinstance(root, McpServerUnavailable):
        return (
            "transport",
            f"MCP server {server} did not answer {what} within {timeout:g} s. The call may still "
            "have taken effect; check before repeating it.",
        )
    if isinstance(root, MCPError):
        if root.code == types.METHOD_NOT_FOUND:
            return ("not_found", f"MCP server {server} does not know {what} ({root.message}).")
        if root.code in _TRANSPORT_CODES:
            return ("transport", f"MCP server {server}: the connection failed ({root.message}).")
        return ("tool_error", f"MCP server {server} answered {what} with an error: {root.message}")
    if isinstance(root, _TRANSPORT_TYPES):
        text = str(root) or type(root).__name__
        return ("transport", f"MCP server {server} could not be reached for {what}: {text}")
    text = f"{type(root).__name__}: {root}" if str(root) else type(root).__name__
    return ("transport", f"MCP call {what} on {server} failed: {text}")


# -- _meta --------------------------------------------------------------------------------------


def is_reserved_meta_key(key: str) -> bool:
    """A ``_meta`` key under a protocol prefix: some label of the part before ``/`` is
    ``modelcontextprotocol`` or ``mcp`` and another label follows it."""
    prefix, slash, _ = key.partition("/")
    if not slash or not prefix:
        return False
    labels = prefix.split(".")
    return any(label in ("modelcontextprotocol", "mcp") for label in labels[:-1])


def drop_reserved_meta(meta: object) -> dict[str, Any] | None:
    if not isinstance(meta, Mapping):
        return None
    kept = {
        k: v
        for k, v in cast(Mapping[object, Any], meta).items()
        if isinstance(k, str) and not is_reserved_meta_key(k)
    }
    return kept or None


# -- media files --------------------------------------------------------------------------------

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _base_mime(mime: object) -> str:
    return str(mime or "").split(";", 1)[0].strip().lower()


def _extension(mime: str, fallback: str) -> str:
    if mime in ("image/jpeg", "image/jpg"):
        return ".jpg"
    if mime in ("audio/wav", "audio/x-wav", "audio/wave"):
        return ".wav"
    return mimetypes.guess_extension(mime) or fallback


def _decode(data: str, *, capped: bool) -> bytes | None:
    if capped and len(data) > _MAX_RESOURCE_B64:
        return None
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None
    if capped and len(raw) > MAX_RESOURCE_BYTES:
        return None
    return raw


def _save(home: Path, folder: str, data: bytes, suffix: str, stem: str = "mcp") -> Path:
    target_dir = home / "cache" / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    path = target_dir / f"{stem}-{digest}{suffix}"
    if not path.exists():
        atomic_write_bytes(path, data)
    return path


def _resource_stem(uri: str) -> str:
    tail = uri.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    cleaned = _SAFE_NAME.sub("_", tail).strip("._")[:60]
    return f"mcp-{cleaned}" if cleaned else "mcp-resource"


# -- rendering ----------------------------------------------------------------------------------


@dataclass
class Rendered:
    """A rendered tool answer: the JSON text, the images it saved, whether it is an error."""

    text: str
    images: list[Path] = field(default_factory=list[Path])
    is_error: bool = False


def _cap(text: str) -> str:
    return head_tail(text, MAX_RESULT_CHARS, label="MCP result")


def _block_text(
    block: object, server: str, home: Path, images: list[Path], own: str = "MEDIA:"
) -> tuple[str, bool]:
    """One content block as text, and whether it carried something real. ``own`` prefixes the
    path of a file this call saved (``render_result`` passes a one-off marker it turns into
    ``MEDIA:`` after the joined text was neutralised)."""
    if isinstance(block, types.TextContent):
        return neutral(block.text), bool(block.text.strip())
    if isinstance(block, types.ImageContent | types.AudioContent):
        kind = "image" if isinstance(block, types.ImageContent) else "audio"
        mime = _base_mime(block.mime_type)
        raw = _decode(block.data, capped=kind == "audio")
        if raw is None:
            log.warning("MCP server %s: an %s block could not be decoded; dropped", server, kind)
            return (
                f"[MCP {kind} could not be decoded or was too large: {mime or 'unknown type'}]",
                True,
            )
        folder = "images" if kind == "image" else "audio"
        try:
            path = _save(home, folder, raw, _extension(mime, ".png" if kind == "image" else ".bin"))
        except OSError as exc:
            log.warning("MCP server %s: could not save an %s block: %s", server, kind, exc)
            return f"[MCP {kind} received ({len(raw)} bytes) but could not be saved]", True
        if kind == "image":
            images.append(path)
        return f"{own}{path}", True
    if isinstance(block, types.EmbeddedResource):
        resource = block.resource
        uri = str(resource.uri)
        mime = _base_mime(resource.mime_type)
        if isinstance(resource, types.TextResourceContents):
            return neutral(resource.text), bool(resource.text.strip())
        raw = _decode(resource.blob, capped=True)
        if raw is None:
            return neutral(f"[MCP resource {uri} could not be decoded or is over 50 MiB]"), True
        try:
            path = _save(home, "documents", raw, _extension(mime, ".bin"), _resource_stem(uri))
        except OSError as exc:
            log.warning("MCP server %s: could not save a resource: %s", server, exc)
            text = f"[MCP resource {uri} received ({len(raw)} bytes) but could not be saved]"
            return neutral(text), True
        kind = mime or "unknown type"
        return neutral(f"[MCP resource {uri} saved to {path} ({kind}, {len(raw)} bytes)]"), True
    if isinstance(block, types.ResourceLink):
        reader = tool_name(server, "read_resource")
        details = f"uri={block.uri}" + (f", name={block.name}" if block.name else "")
        if block.mime_type:
            details += f", mimeType={block.mime_type}"
        return neutral(f"[MCP resource link: {details}; read it with {reader}]"), True
    kind = getattr(block, "type", None) or type(block).__name__
    log.warning("MCP server %s: dropped a content block of type %r", server, kind)
    return neutral(f"[MCP content dropped: unsupported block type {kind}]"), False


def _dual_emit(blocks: Sequence[object], structured: object) -> bool:
    for block in blocks:
        if not isinstance(block, types.TextContent) or not block.text.strip():
            continue
        try:
            if json.loads(block.text) == structured:
                return True
        except ValueError:
            continue
    return False


def render_result(result: types.CallToolResult, *, server: str, home: Path) -> Rendered:
    """The tool answer as JSON text (see the module notes)."""
    images: list[Path] = []
    blocks: list[object] = list(result.content or [])
    if result.is_error:
        parts = [
            neutral(b.text) for b in blocks if isinstance(b, types.TextContent) and b.text.strip()
        ]
        message = _cap("\n".join(parts)) if parts else f"MCP server {server} reported an error"
        return Rendered(_error_json(message, "tool_error", server), [], True)
    texts: list[str] = []
    usable = 0
    # Our own MEDIA lines carry a marker no server can know, so the joined text can be
    # neutralised as a whole (a directive split across two blocks is caught too).
    own = f"\ue000{secrets.token_hex(8)}\ue001"
    for block in blocks:
        text, real = _block_text(block, server, home, images, own)
        texts.append(text)
        usable += 1 if real else 0
    body = _cap(neutral("\n".join(texts))).replace(own, "MEDIA:")
    structured: object = result.structured_content
    if structured is not None:
        try:
            as_json = json.dumps(structured, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            as_json = ""
        if len(as_json) > MAX_RESULT_CHARS:
            structured = _cap(as_json) if usable == 0 else None
        elif usable and _dual_emit(blocks, structured):
            structured = None
        structured = _neutral_tree(structured)
    meta = drop_reserved_meta(result.meta)
    if meta is not None:
        meta = cast(dict[str, Any], _neutral_tree(meta))
    payload: dict[str, Any] = {}
    if usable or structured is None:
        payload["result"] = body
        if structured is not None:
            payload["structuredContent"] = structured
    else:
        payload["result"] = structured
    if meta is not None:
        payload["_meta"] = meta
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps({"result": body}, ensure_ascii=False)
    return Rendered(text, images, False)


# -- native images ------------------------------------------------------------------------------


def _fit_image(path: Path, scope: Scope | None) -> dict[str, Any] | None:
    from indus.tools.builtin.media import imaging
    from indus.tools.builtin.media.vision import EMBED_MAX_SIDE, embed_target_bytes

    try:
        data = path.read_bytes()
        info = imaging.inspect(data)
        fitted = imaging.fit(
            data, info, max_side=EMBED_MAX_SIDE, max_b64_bytes=embed_target_bytes(scope)
        )
    except Exception as exc:  # a bad or unshrinkable image keeps its path only
        log.debug("MCP image %s not attached: %s", path, exc)
        return None
    return {"type": "image_url", "image_url": {"url": imaging.data_url(fitted.data, fitted.mime)}}


async def with_native_images(text: str, images: Sequence[Path], scope: Scope | None) -> Any:
    """``text``, or the multimodal envelope with up to 4 of ``images`` attached."""
    if not images:
        return text
    attached: list[dict[str, Any]] = []
    for path in images[:NATIVE_IMAGE_CANDIDATES]:
        if len(attached) >= MAX_NATIVE_IMAGES:
            break
        part = await run_blocking(_fit_image, path, scope, pool="tools", label="mcp image")
        if part is not None:
            attached.append(part)
    if not attached:
        return text
    note = f"{len(attached)} image(s) from this call are attached below" + (
        f"; {len(images) - len(attached)} more stay as MEDIA paths."
        if len(images) > len(attached)
        else "."
    )
    return {
        "multimodal": True,
        "content": [{"type": "text", "text": text}, {"type": "text", "text": note}, *attached],
        "text_summary": text,
    }


# -- utilities ----------------------------------------------------------------------------------


async def _paged(fetch: Any, attr: str) -> list[Any]:
    found: list[Any] = []
    cursor: str | None = None
    for _ in range(LIST_MAX_PAGES):
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await fetch(params=params)
        found.extend(getattr(page, attr, None) or [])
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            break
    return found


def _resource_rows(resources: Sequence[Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in resources:
        row: dict[str, Any] = {"uri": neutral(str(item.uri)), "name": neutral(item.name)}
        if item.description:
            row["description"] = neutral(item.description)
        if item.mime_type:
            row["mimeType"] = item.mime_type
        rows.append(row)
    return {"resources": rows}


def _read_rows(result: Any, server: str, home: Path) -> dict[str, Any]:
    parts: list[str] = []
    for item in getattr(result, "contents", None) or []:
        block = types.EmbeddedResource(type="resource", resource=item)
        parts.append(_block_text(block, server, home, [])[0])
    return {"result": _cap("\n".join(parts))}


def _prompt_rows(prompts: Sequence[Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in prompts:
        row: dict[str, Any] = {"name": neutral(item.name)}
        if item.description:
            row["description"] = neutral(item.description)
        if item.arguments:
            row["arguments"] = [
                {
                    "name": neutral(a.name),
                    "description": neutral(a.description or ""),
                    "required": bool(a.required),
                }
                for a in item.arguments
            ]
        rows.append(row)
    return {"prompts": rows}


def _prompt_messages(result: Any) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for message in getattr(result, "messages", None) or []:
        content = message.content
        text = content.text if isinstance(content, types.TextContent) else str(content)
        messages.append({"role": message.role, "content": neutral(text)})
    out: dict[str, Any] = {"messages": messages}
    description = getattr(result, "description", None)
    if description:
        out["description"] = neutral(description)
    return out


async def _utility(
    live: LiveSession, spec: ToolSpec, args: Mapping[str, Any], server: str, home: Path
) -> str:
    raw = live.raw
    if spec.kind == "list_resources":
        body = _resource_rows(await _paged(raw.list_resources, "resources"))
    elif spec.kind == "read_resource":
        body = _read_rows(await raw.read_resource(str(args["uri"])), server, home)
    elif spec.kind == "list_prompts":
        body = _prompt_rows(await _paged(raw.list_prompts, "prompts"))
    else:
        arguments = args.get("arguments")
        named = (
            {str(k): str(v) for k, v in cast(Mapping[object, object], arguments).items()}
            if isinstance(arguments, Mapping)
            else None
        )
        body = _prompt_messages(await raw.get_prompt(str(args["name"]), named))
    return json.dumps(body, ensure_ascii=False)


_REQUIRED: dict[str, str] = {"read_resource": "uri", "get_prompt": "name"}
# Run contexts in which nobody can answer a server's elicitation (it is declined).
_UNATTENDED = frozenset({"cron", "single_query", "unattended"})


# -- the call -----------------------------------------------------------------------------------


async def call(
    target: CallTarget, spec: ToolSpec, args: Mapping[str, Any], ctx: CallContext
) -> Any:
    """Run one registered MCP tool; returns the handler result (text or the envelope)."""
    server = target.name
    what = spec.server_tool if spec.kind == "tool" else spec.kind
    needed = _REQUIRED.get(spec.kind)
    if needed is not None and not args.get(needed):
        return _error_json(
            f"{spec.registry_name} needs the argument {needed!r}", "bad_args", server
        )
    if spec.kind == "tool":
        refusal = await trust.gate_call(
            server,
            spec.server_tool,
            trust=target.config.trust,
            is_read_only=spec.read_only,
            scope=ctx.scope,
            session_id=ctx.session_id,
            turn_id=ctx.turn_id,
            broker=ctx.broker,
            run_context=ctx.run_context,
            judge=ctx.judge,
            state=ctx.approvals,
        )
        if refusal is not None:
            return _error_json(refusal, "not_approved", server)
    breaker = target.breaker
    blocked = breaker.check()
    if blocked is not None:
        return _error_json(blocked, "breaker_open", server)
    timeout = target.config.timeout
    recorded = False

    def record(ok: bool, *, application: bool = False) -> None:
        nonlocal recorded
        recorded = True
        breaker.record(ok, application=application)

    requesters = getattr(target, "requesters", None)
    serving: contextlib.AbstractContextManager[object] = (
        requesters.serving(
            ctx.broker,
            unattended=ctx.broker is None or ctx.run_context in _UNATTENDED,
            turn_id=ctx.turn_id,
        )
        if requesters is not None
        else contextlib.nullcontext()
    )
    try:
        async with (
            target.slot(),
            target.session() as live,
            clock_timeout(target.clock, timeout),
        ):
            if spec.kind != "tool":
                text = await _utility(live, spec, args, server, target.home)
                record(True)
                return text
            with serving:
                raw: object = await live.call_tool(spec.server_tool, dict(args))
        if not isinstance(raw, types.CallToolResult):
            record(False, application=True)
            return _error_json(
                f"MCP server {server} answered {what} with something other than a tool result",
                "tool_error",
                server,
            )
        answer = raw
        rendered = await run_blocking(
            lambda: render_result(answer, server=server, home=target.home),
            pool="io",
            label="mcp result",
        )
        if rendered.is_error:
            record(False, application=True)
            return rendered.text
        record(True)
        if ctx.native_vision and rendered.images:
            return await with_native_images(rendered.text, rendered.images, ctx.scope)
        return rendered.text
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        kind, message = classify(exc, server, what, timeout)
        log.warning("MCP %s on %s failed (%s): %s", what, server, kind, message)
        record(False, application=kind in ("tool_error", "not_found"))
        return _error_json(message, kind, server)
    finally:
        if not recorded:
            breaker.record(None)  # cancelled: give a half-open probe slot back
