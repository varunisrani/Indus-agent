"""``vision_analyze``: look at an image and answer a question about it (toolset ``vision``).

Load (``sources.load_source``: URL policy, 50 MB, ``vision.download_timeout``) → type by magic
bytes and the animation caps (``imaging``) → optional region crop at original resolution → route:

- **native**: the host said the main model sees images (``native_vision``). The image is shrunk to
  at most ``EMBED_MAX_SIDE`` px and to the embed target (``vision.embed_target_kb``, clamped to
  64–4,096 KB) and returned as the multimodal envelope, which the main model reads directly. That
  image rides along on every later call of the session, so inside a delegated child each source
  may be embedded at most ``CHILD_EMBED_CAP`` times per session (``EmbedBudget``); later calls
  take the text route.
- **text**: the auxiliary model (task ``vision``) gets the question and the image and its answer
  is returned as JSON.

Host keywords the handler reads (offered by the tool round when the host has them): ``aux`` (an
object with ``AuxClient.call``'s shape), ``native_vision`` (bool), ``subagent`` (bool, inside a
delegated child) and ``session_id``.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.safety.safe_transport import TransportFactory
from indus.safety.url_policy import Resolver
from indus.tools.builtin.media import imaging
from indus.tools.builtin.media.common import error_text, json_text, setting_number
from indus.tools.builtin.media.sources import Loaded, SourceError, load_source
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "CHILD_EMBED_CAP",
    "EMBED_MAX_SIDE",
    "MAX_SOURCE_BYTES",
    "SCHEMA",
    "TOOLSET",
    "EmbedBudget",
    "VisionTool",
    "embed_target_bytes",
]

log = logging.getLogger(__name__)

TOOLSET = "vision"
AUX_TASK = "vision"
MAX_SOURCE_BYTES = 50 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_S = 30.0
EMBED_MAX_SIDE = 1568
DEFAULT_EMBED_TARGET_KB = 256
MIN_EMBED_TARGET_KB = 64
MAX_EMBED_TARGET_KB = 4096
CHILD_EMBED_CAP = 3
BUDGET_MAX_KEYS = 4096
# The text route sends a bounded image too (side-task models reject huge payloads).
AUX_MAX_SIDE = 2048
AUX_MAX_B64_BYTES = 5 * 1024 * 1024
DEFAULT_QUESTION = "Describe this image in detail."
MAX_RESULT_CHARS = 40_000

SCHEMA: dict[str, Any] = {
    "name": "vision_analyze",
    "description": (
        "Look at an image and answer a question about it. The image can be a web address "
        "(http/https), a local file path or a data: URL; internal network addresses are refused. "
        "PNG, JPEG, GIF and WebP are accepted (the type is read from the file itself). Pass "
        "region [x1, y1, x2, y2] in the original image's pixels to look closely at one part at "
        "full resolution. When the answer mentions coordinates of a shrunk or cropped image, the "
        "result says how to map them back."
    ),
    "parameters": {
        "type": "object",
        "required": ["image_url", "question"],
        "properties": {
            "image_url": {
                "type": "string",
                "description": "Where the image is: http(s) URL, local path, file:// URI or "
                "data: URL.",
            },
            "question": {
                "type": "string",
                "description": "What you want to know about the image.",
            },
            "region": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "Optional [x1, y1, x2, y2] box in original image pixels to cut "
                "out before looking.",
            },
        },
    },
}


class AuxCaller(Protocol):
    async def call(self, task: str, messages: Any, /, **options: Any) -> Any: ...


def embed_target_bytes(scope: Scope | None) -> int:
    kb = setting_number(scope, "vision.embed_target_kb", DEFAULT_EMBED_TARGET_KB)
    kb = min(max(kb, MIN_EMBED_TARGET_KB), MAX_EMBED_TARGET_KB)
    return int(kb * 1024)


class EmbedBudget:
    """Native embeds per (session, source) inside children, with a bounded table.

    ``reserve`` checks and counts under one lock (parallel calls on one image cannot all pass);
    ``release`` gives a slot back when the embed did not happen.
    """

    def __init__(self, cap: int = CHILD_EMBED_CAP, max_keys: int = BUDGET_MAX_KEYS) -> None:
        self.cap = cap
        self.max_keys = max_keys
        self._counts: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._counts)

    def count(self, session: str, key: str) -> int:
        with self._lock:
            return self._counts.get((session, key), 0)

    def reserve(self, session: str, key: str) -> bool:
        slot = (session, key)
        with self._lock:
            used = self._counts.get(slot, 0)
            if used >= self.cap:
                return False
            self._counts[slot] = used + 1
            self._counts.move_to_end(slot)
            while len(self._counts) > self.max_keys:
                self._counts.popitem(last=False)
            return True

    def release(self, session: str, key: str) -> None:
        slot = (session, key)
        with self._lock:
            used = self._counts.get(slot, 0)
            if used > 1:
                self._counts[slot] = used - 1
            else:
                self._counts.pop(slot, None)


@dataclass(frozen=True)
class Prepared:
    data: bytes
    info: imaging.ImageInfo
    loaded: Loaded
    offset: tuple[int, int] | None  # crop origin in the original image


def _region_of(args: Mapping[str, Any]) -> Any:
    region = args.get("region")
    return None if region in (None, [], "") else region


def _scale_note(fitted: imaging.Fitted | None, offset: tuple[int, int] | None) -> str:
    notes: list[str] = []
    if fitted is not None and fitted.scaled_from is not None:
        ow, oh = fitted.scaled_from
        notes.append(
            f"The image was shrunk from {ow}x{oh} to {fitted.width}x{fitted.height} px; "
            f"multiply x by {ow / fitted.width:.3f} and y by {oh / fitted.height:.3f} to get "
            "original coordinates."
        )
    if offset is not None:
        notes.append(
            f"This is a cut-out whose top-left corner is ({offset[0]}, {offset[1]}) in the "
            "original image; add that offset to coordinates inside it."
        )
    return " ".join(notes)


_BILLING = ("402", "billing", "insufficient", "credit", "payment required", "quota")
_NO_VISION = ("does not support", "not support image", "image input", "multimodal", "vision")
_SIZE = ("too large", "413", "payload", "request_too_large", "exceeds")


def _hint_for(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    status = getattr(exc, "status", None)
    if status == 402 or any(word in text for word in _BILLING):
        return (
            "The side-task model refused for billing reasons (out of credits or no payment set "
            "up). Top up that provider account or set auxiliary.vision to another provider."
        )
    if status == 413 or any(word in text for word in _SIZE):
        return "The image was too large for the side-task model. Try a region or a smaller image."
    if any(word in text for word in _NO_VISION):
        return (
            "The side-task model cannot read images. Set auxiliary.vision.provider and "
            "auxiliary.vision.model to a model that accepts images."
        )
    return f"The side-task model could not answer: {exc}"


class VisionTool:
    """The ``vision_analyze`` handler and what it keeps between calls."""

    def __init__(
        self,
        *,
        budget: EmbedBudget | None = None,
        resolver: Resolver | None = None,
        inner_factory: TransportFactory | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.budget = budget if budget is not None else EmbedBudget()
        self.resolver = resolver
        self.inner_factory = inner_factory
        self.cwd = cwd

    async def _prepare(self, args: Mapping[str, Any], scope: Scope | None) -> Prepared:
        loaded = await load_source(
            str(args.get("image_url") or ""),
            scope,
            max_bytes=MAX_SOURCE_BYTES,
            timeout_s=setting_number(scope, "vision.download_timeout", DEFAULT_DOWNLOAD_TIMEOUT_S),
            cwd=self.cwd,
            resolver=self.resolver,
            inner_factory=self.inner_factory,
        )
        data = loaded.data
        info = imaging.inspect(data)
        imaging.check_limits(info)
        await run_blocking(imaging.validate_decodable, data, info, pool="tools", label="vision")
        if info.mime == "image/bmp":
            data = await run_blocking(imaging.to_png, data, pool="tools", label="vision")
            info = imaging.inspect(data)
        offset: tuple[int, int] | None = None
        region = _region_of(args)
        if region is not None:
            box = imaging.clamp_box(region, info)
            data = await run_blocking(imaging.crop, data, info, box, pool="tools", label="vision")
            info = imaging.inspect(data)
            offset = (box[0], box[1])
        return Prepared(data, info, loaded, offset)

    async def analyze(
        self,
        args: Mapping[str, Any],
        *,
        scope: Scope | None = None,
        session_id: str | None = None,
        aux: AuxCaller | None = None,
        native_vision: bool = False,
        subagent: bool = False,
    ) -> Any:
        question = str(args.get("question") or "").strip() or DEFAULT_QUESTION
        if not str(args.get("image_url") or "").strip():
            return error_text("image_url is required")
        try:
            prepared = await self._prepare(args, scope)
        except (SourceError, imaging.BadImage) as exc:
            return error_text(f"vision_analyze could not use the image: {exc}")
        note = ""
        if native_vision:
            session = session_id or ""
            counted = subagent
            if counted and not self.budget.reserve(session, prepared.loaded.key):
                note = (
                    f"This image was already shown to you {self.budget.cap} times in this "
                    "session, so this answer comes from a side model as text."
                )
            else:
                try:
                    return await self._embed(prepared, question, scope)
                except imaging.BadImage as exc:
                    if counted:
                        self.budget.release(session, prepared.loaded.key)
                    note = f"The image could not be embedded ({exc}); a side model looked at it."
        return await self._ask_aux(prepared, question, aux, note)

    async def _embed(self, prepared: Prepared, question: str, scope: Scope | None) -> Any:
        fitted = await run_blocking(
            lambda: imaging.fit(
                prepared.data,
                prepared.info,
                max_side=EMBED_MAX_SIDE,
                max_b64_bytes=embed_target_bytes(scope),
            ),
            pool="tools",
            label="vision",
        )
        note = _scale_note(fitted, prepared.offset)
        text = "The image is attached below; look at it to answer.\n\nQuestion: " + question
        if note:
            text += "\n\nNote: " + note
        size_kb = len(fitted.data) / 1024
        return {
            "multimodal": True,
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {"url": imaging.data_url(fitted.data, fitted.mime)},
                },
            ],
            "text_summary": (
                f"[image {fitted.width}x{fitted.height} px, {size_kb:.1f} KB, from "
                f"{prepared.loaded.label} — shown to the model directly]"
                + (f" {note}" if note else "")
            ),
        }

    async def _ask_aux(
        self, prepared: Prepared, question: str, aux: AuxCaller | None, note: str
    ) -> str:
        if aux is None:
            if note:
                return error_text(
                    f"{note} No side-task model is available, so: answer from "
                    "what you have already seen."
                )
            return error_text(
                "vision_analyze needs a model that can read images, and none is set up for this "
                "session: the main model is not marked as vision-capable and no side-task model "
                "is available (configure auxiliary.vision)."
            )
        try:
            fitted = await run_blocking(
                lambda: imaging.fit(
                    prepared.data,
                    prepared.info,
                    max_side=AUX_MAX_SIDE,
                    max_b64_bytes=AUX_MAX_B64_BYTES,
                ),
                pool="tools",
                label="vision",
            )
        except imaging.BadImage as exc:
            if imaging.b64_len(len(prepared.data)) > AUX_MAX_B64_BYTES:
                return error_text(f"vision_analyze could not use the image: {exc}")
            fitted = imaging.Fitted(
                prepared.data, prepared.info.mime, prepared.info.width, prepared.info.height
            )
        scale = _scale_note(fitted, prepared.offset)
        prompt = question
        if scale:
            prompt += "\n\n(" + scale + ")"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": imaging.data_url(fitted.data, fitted.mime)},
                    },
                ],
            }
        ]
        try:
            reply = await aux.call(AUX_TASK, messages, temperature=0.1)
        except Exception as exc:
            log.info("vision side call failed: %s: %s", type(exc).__name__, exc)
            return error_text(_hint_for(exc))
        text = str(getattr(reply, "text", "") or "").strip()
        if not text:
            return error_text("The side-task model gave an empty answer; try again or rephrase.")
        body: dict[str, Any] = {"success": True, "analysis": text}
        notes = " ".join(n for n in (note, scale) if n)
        if notes:
            body["note"] = notes
        return json_text(body)

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "vision_analyze",
            TOOLSET,
            SCHEMA,
            self.analyze,
            max_result_chars=MAX_RESULT_CHARS,
            traits=ToolTraits(read_only=True),
        )


DEFAULT_TOOL = VisionTool()
