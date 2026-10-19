"""OpenAI wire shapes for the API server (P15-30): request checks, answers, SSE frames.

Pure functions, no I/O. Field and event names follow OpenAI's public OpenAPI document (pinned
commit in ``docs/notes/P15/api-server-openai.md``); the tests validate what these functions build
against schema pieces extracted from it.

Request content arrives as a string or as an array of typed parts. ``normalise_content`` turns it
into ``Content``: the text parts joined (each part capped at ``MAX_TEXT_PART`` characters), plus
the images (http(s) URLs or ``data:image/...`` URLs of at most ``MAX_IMAGE_DATA_URL`` bytes). An
array longer than ``MAX_CONTENT_ITEMS``, a file part or an unknown part type is refused with a
``WireError`` that carries the HTTP status and the OpenAI error code.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

__all__ = [
    "MAX_CONTENT_ITEMS",
    "MAX_IMAGE_DATA_URL",
    "MAX_TEXT_PART",
    "ChatRequest",
    "Content",
    "ImageRef",
    "ResponsesRequest",
    "WireError",
    "chat_chunk",
    "chat_completion",
    "coerce_bool",
    "error_body",
    "model_list",
    "normalise_content",
    "parse_chat_request",
    "parse_responses_request",
    "response_object",
    "sse_comment",
    "sse_frame",
]

MAX_TEXT_PART = 64 * 1024
"""Characters kept from one text part (64 KiB)."""
MAX_CONTENT_ITEMS = 1_000
"""Most parts in one content array."""
MAX_IMAGE_DATA_URL = 5 * 1024 * 1024
"""Largest ``data:image/...`` URL accepted, in bytes (5 MiB)."""
MAX_MESSAGES = 1_000

_TEXT_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_TYPES = frozenset({"image_url", "input_image"})
_FILE_TYPES = frozenset({"file", "input_file"})
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class WireError(ValueError):
    """A request the API refuses: HTTP status, OpenAI error type/code and the offending param."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: str | None = None,
        param: str | None = None,
        kind: str = "invalid_request_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.param = param
        self.kind = kind

    def body(self) -> dict[str, Any]:
        return error_body(self.message, kind=self.kind, code=self.code, param=self.param)


def error_body(
    message: str,
    *,
    kind: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    """The OpenAI error envelope."""
    return {"error": {"message": message, "type": kind, "param": param, "code": code}}


def coerce_bool(value: object, default: bool = False) -> bool:
    """A request flag: real booleans, numbers, and the usual words (``"false"`` is false)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    return default


# -- content ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageRef:
    """An image the client sent: an http(s) URL, or a data URL with its media type."""

    url: str
    media_type: str | None = None  # set for data URLs

    @property
    def is_data(self) -> bool:
        return self.media_type is not None


@dataclass(frozen=True, slots=True)
class Content:
    text: str = ""
    images: tuple[ImageRef, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.text.strip() and not self.images


def _cap(text: str) -> str:
    return text if len(text) <= MAX_TEXT_PART else text[:MAX_TEXT_PART]


def _image(part: Mapping[str, Any], param: str) -> ImageRef:
    ref = part.get("image_url")
    url = cast(Mapping[str, Any], ref).get("url") if isinstance(ref, Mapping) else ref
    if not isinstance(url, str) or not url.strip():
        raise WireError("an image part needs an image URL", code="invalid_image_url", param=param)
    url = url.strip()
    lowered = url[:16].lower()
    if lowered.startswith("data:"):
        head, comma, _ = url.partition(",")
        if not comma or not head.lower().startswith("data:image/"):
            raise WireError(
                "only image data URLs are accepted", code="unsupported_content_type", param=param
            )
        if len(url.encode("utf-8")) > MAX_IMAGE_DATA_URL:
            raise WireError(
                f"an image data URL may be at most {MAX_IMAGE_DATA_URL} bytes",
                status=413,
                code="image_too_large",
                param=param,
            )
        media = head[5:].split(";", 1)[0].strip().lower()
        return ImageRef(url, media)
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise WireError(
            "image URLs must be http(s) or data:image/ URLs", code="invalid_image_url", param=param
        )
    return ImageRef(url)


def normalise_content(raw: object, param: str = "content") -> Content:
    """See the module notes. ``None`` is empty content; other scalars are read as text."""
    if raw is None:
        return Content()
    if isinstance(raw, str):
        return Content(_cap(raw))
    if not isinstance(raw, list):
        return Content(_cap(str(raw)))
    items = cast(list[Any], raw)
    if len(items) > MAX_CONTENT_ITEMS:
        raise WireError(
            f"a content array may hold at most {MAX_CONTENT_ITEMS} parts",
            code="too_many_content_parts",
            param=param,
        )
    texts: list[str] = []
    images: list[ImageRef] = []
    for index, part in enumerate(items):
        where = f"{param}[{index}]"
        if isinstance(part, str):
            if part:
                texts.append(_cap(part))
            continue
        if not isinstance(part, Mapping):
            continue  # unknown scalars are skipped
        fields = cast(Mapping[str, Any], part)
        kind = str(fields.get("type") or "").strip().lower()
        if kind in _TEXT_TYPES:
            text = fields.get("text")
            if text is not None and str(text):
                texts.append(_cap(str(text)))
        elif kind in _IMAGE_TYPES:
            images.append(_image(fields, where))
        elif kind in _FILE_TYPES:
            raise WireError(
                "file inputs are not accepted here; send text or images",
                code="unsupported_content_type",
                param=where,
            )
        else:
            raise WireError(
                f"unsupported content part type {fields.get('type')!r}",
                code="unsupported_content_type",
                param=where,
            )
    return Content("\n".join(texts), tuple(images))


# -- requests -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: Content


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str | None
    stream: bool
    system: str
    history: tuple[Message, ...]
    user: Content


@dataclass(frozen=True, slots=True)
class ResponsesRequest:
    model: str | None
    stream: bool
    store: bool
    instructions: str
    previous_response_id: str | None
    history: tuple[Message, ...]
    user: Content
    raw_input: Any = field(default=None, compare=False)


def _messages(raw: object, param: str) -> list[Message]:
    if not isinstance(raw, list) or not raw:
        raise WireError(
            f"'{param}' must be a non-empty array", code="invalid_messages", param=param
        )
    items = cast(list[Any], raw)
    if len(items) > MAX_MESSAGES:
        raise WireError(
            f"at most {MAX_MESSAGES} messages per request", code="invalid_messages", param=param
        )
    out: list[Message] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            out.append(Message("user", normalise_content(item, f"{param}[{index}]")))
            continue
        if not isinstance(item, Mapping):
            raise WireError(
                f"{param}[{index}] must be an object", code="invalid_messages", param=param
            )
        fields = cast(Mapping[str, Any], item)
        if fields.get("type") == "reasoning":
            continue
        role = str(fields.get("role") or "user")
        out.append(Message(role, normalise_content(fields.get("content"), f"{param}[{index}]")))
    return out


def _split(messages: Sequence[Message], param: str) -> tuple[str, tuple[Message, ...], Content]:
    system = "\n".join(
        m.content.text for m in messages if m.role in {"system", "developer"} and m.content.text
    )
    talk = [m for m in messages if m.role in {"user", "assistant"}]
    if not talk or talk[-1].role != "user" or talk[-1].content.empty:
        raise WireError("the last message must be a non-empty user message", param=param)
    return system, tuple(talk[:-1]), talk[-1].content


def _model(body: Mapping[str, Any]) -> str | None:
    model = body.get("model")
    return model.strip() or None if isinstance(model, str) else None


def parse_chat_request(body: object) -> ChatRequest:
    if not isinstance(body, Mapping):
        raise WireError("the request body must be a JSON object")
    fields = cast(Mapping[str, Any], body)
    system, history, user = _split(_messages(fields.get("messages"), "messages"), "messages")
    return ChatRequest(_model(fields), coerce_bool(fields.get("stream")), system, history, user)


def parse_responses_request(body: object) -> ResponsesRequest:
    if not isinstance(body, Mapping):
        raise WireError("the request body must be a JSON object")
    fields = cast(Mapping[str, Any], body)
    raw = fields.get("input")
    if raw is None:
        raise WireError("'input' is required", param="input")
    if isinstance(raw, str):
        messages = [Message("user", normalise_content(raw, "input"))]
    elif isinstance(raw, list):
        messages = _messages(raw, "input")
    else:
        raise WireError("'input' must be a string or an array", param="input")
    system, history, user = _split(messages, "input")
    instructions = fields.get("instructions")
    previous = fields.get("previous_response_id")
    return ResponsesRequest(
        model=_model(fields),
        stream=coerce_bool(fields.get("stream")),
        store=coerce_bool(fields.get("store"), default=True),
        instructions="\n".join(
            p for p in (instructions if isinstance(instructions, str) else "", system) if p
        ),
        previous_response_id=previous if isinstance(previous, str) and previous else None,
        history=history,
        user=user,
        raw_input=raw,
    )


# -- answers ------------------------------------------------------------------------------------


def chat_usage(prompt: int, completion: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def responses_usage(prompt: int, completion: int) -> dict[str, Any]:
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": prompt + completion,
    }


def chat_completion(
    *,
    completion_id: str,
    created: int,
    model: str,
    text: str,
    finish_reason: str,
    usage: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "refusal": None},
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
        "usage": dict(usage),
    }


def chat_chunk(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: Mapping[str, Any],
    finish_reason: str | None = None,
    usage: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = dict(usage)
    return chunk


def model_list(model: str, created: int) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": model, "object": "model", "created": created, "owned_by": "indus"}],
    }


def message_item(item_id: str, text: str, status: str = "completed") -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": status,
        "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
    }


def response_object(
    *,
    response_id: str,
    created_at: int,
    model: str,
    status: str,
    output: Sequence[Mapping[str, Any]] = (),
    usage: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
    instructions: str | None = None,
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    """A Responses ``response`` object with the fields clients read."""
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": [dict(item) for item in output],
        "usage": dict(usage) if usage is not None else None,
        "error": dict(error) if error is not None else None,
        "incomplete_details": None,
        "instructions": instructions or None,
        "previous_response_id": previous_response_id,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "metadata": {},
        "temperature": None,
        "top_p": None,
        "access_programs": None,
    }


# -- SSE ----------------------------------------------------------------------------------------


def sse_frame(data: object, *, event: str | None = None, event_id: int | None = None) -> bytes:
    """One SSE frame: optional ``id:`` and ``event:`` lines, then ``data: <json>``."""
    head = ""
    if event_id is not None:
        head += f"id: {event_id}\n"
    if event:
        head += f"event: {event}\n"
    return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def sse_comment(text: str) -> bytes:
    """An SSE comment line (clients ignore it; it keeps idle connections open)."""
    return f": {text}\n\n".encode()


SSE_DONE = b"data: [DONE]\n\n"
