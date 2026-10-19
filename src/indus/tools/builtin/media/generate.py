"""``image_generate`` and the video tools over the provider registries (P10-16).

``image_generate`` (toolset ``image_gen``) is always registered; whether it shows depends on its
probe (the active image provider is ready). ``video_generate``, ``video_edit`` and
``video_extend`` (toolset ``video_gen``, opt-in) are registered only while some registered video
provider declares the matching capability; ``GenerationTools`` re-checks that on every change to
the video registry, for every tool registry it was registered into. Each tool's description and
optional parameters are rebuilt from the active provider's capabilities (``schema_overrides``).

The provider and model are the user's (``image_gen.provider`` / ``image_gen.model``,
``media.video_provider`` / ``media.video_model``); a ``model`` argument from the agent is
ignored. Input images and videos are loaded under the media source rules (URL policy, secret-file
guard, size cap). At most ``image_gen.max_parallel_requests`` image requests run at once per
profile. Bytes and file results are saved under ``<home>/cache/media/``; a URL result is returned
as given. When the terminal runs in a container the result also says where the saved file sits
inside the sandbox (``sandbox_path``).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import weakref
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from indus.foundation.blocking import run_blocking
from indus.foundation.fsio import atomic_write_bytes
from indus.foundation.scope import Scope
from indus.safety.safe_transport import TransportFactory
from indus.safety.url_policy import Blocked, Resolver, check_url_async
from indus.tools.builtin.media import imaging
from indus.tools.builtin.media.common import (
    MEDIA_DIR,
    error_text,
    folder,
    json_text,
    sandbox_path,
    setting_number,
    setting_text,
    unique_name,
)
from indus.tools.builtin.media.providers import (
    ImageProvider,
    ImageRequest,
    InputMedia,
    MediaProviders,
    MediaResult,
    NoProvider,
    ProviderFailed,
    VideoProvider,
    VideoRequest,
    active,
    default_providers,
)
from indus.tools.builtin.media.sources import SourceError, load_source
from indus.tools.registry import ToolRegistry, ToolTraits, scope_key

__all__ = ["IMAGE_TOOLSET", "VIDEO_TOOLSET", "VIDEO_TOOLS", "GenerationTools"]

log = logging.getLogger(__name__)

IMAGE_TOOLSET = "image_gen"
VIDEO_TOOLSET = "video_gen"
IMAGE_KEY = "image_gen.provider"
VIDEO_KEY = "media.video_provider"
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_VIDEO_INPUT_BYTES = 200 * 1024 * 1024
DEFAULT_PARALLEL = 4
MAX_GATES = 64

Operation = Literal["generate", "edit", "extend"]
# tool name → (operation, capability flag on VideoCapabilities; None = any video provider)
VIDEO_TOOLS: dict[str, tuple[Operation, str | None]] = {
    "video_generate": ("generate", None),
    "video_edit": ("edit", "edit"),
    "video_extend": ("extend", "extend"),
}

_IMAGE_BASE = (
    "Create an image from a text description and save it. The image service and model are "
    "chosen in the user's settings, not by you. Returns the saved file's absolute path (or a "
    "URL); show it to the user the way this surface delivers files."
)
_VIDEO_BASE = {
    "generate": "Create a short video from a text description",
    "edit": "Change an existing video as the prompt describes",
    "extend": "Continue an existing video past its end as the prompt describes",
}


def _prompt_param(text: str) -> dict[str, Any]:
    return {"type": "string", "description": text}


def _image_schema(provider: ImageProvider | None) -> dict[str, Any]:
    caps = provider.capabilities if provider is not None else None
    ratios = list(caps.aspect_ratios) if caps is not None else ["landscape", "square", "portrait"]
    props: dict[str, Any] = {
        "prompt": _prompt_param("What the image should show, in detail (or the change to make)."),
        "aspect_ratio": {
            "type": "string",
            "enum": ratios,
            "default": ratios[0],
            "description": "Shape of the image.",
        },
    }
    parts = [_IMAGE_BASE]
    if provider is None:
        parts.append("No image service is set up yet; calls will say what to configure.")
    else:
        parts.append(f"Active service: {provider.title}.")
        if caps is not None and caps.edit:
            parts.append("It can change an existing image: pass it as image_url.")
            props["image_url"] = _prompt_param("An image to change (URL, local path or data: URL).")
        if caps is not None and caps.max_reference_images > 0:
            parts.append(
                f"It accepts up to {caps.max_reference_images} reference images for style or "
                "content (reference_image_urls)."
            )
            props["reference_image_urls"] = {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": caps.max_reference_images,
                "description": "Images to take style or content from.",
            }
    return {
        "description": " ".join(parts),
        "parameters": {"type": "object", "required": ["prompt"], "properties": props},
    }


def _video_schema(name: str, provider: VideoProvider | None) -> dict[str, Any]:
    operation = VIDEO_TOOLS[name][0]
    caps = provider.capabilities if provider is not None else None
    parts = [
        _VIDEO_BASE[operation]
        + ". The video service and model are chosen in the user's settings. A call can take "
        "minutes. Returns the saved file's absolute path (or a URL)."
    ]
    props: dict[str, Any] = {"prompt": _prompt_param("What should happen in the video.")}
    if operation != "generate":
        props["video_url"] = _prompt_param("The video to work on (URL, local path or data: URL).")
        required = ["video_url", "prompt"]
    else:
        required = ["prompt"]
    if provider is not None and caps is not None:
        parts.append(f"Active service: {provider.title}.")
        if operation == "generate":
            props["aspect_ratio"] = {
                "type": "string",
                "enum": list(caps.aspect_ratios),
                "description": "Shape of the video.",
            }
            props["resolution"] = {
                "type": "string",
                "enum": list(caps.resolutions),
                "description": "Output resolution.",
            }
            parts.append(
                f"Aspect ratios: {', '.join(caps.aspect_ratios)}; resolutions: "
                f"{', '.join(caps.resolutions)}."
            )
            if caps.durations:
                props["duration"] = {
                    "type": "integer",
                    "enum": list(caps.durations),
                    "description": "Length in seconds.",
                }
                parts.append(
                    "Durations: " + ", ".join(str(d) for d in caps.durations) + " seconds."
                )
            if caps.image_input:
                props["image_url"] = _prompt_param("A still image to animate.")
                parts.append("It can animate a still image passed as image_url.")
    return {
        "description": " ".join(parts),
        "parameters": {"type": "object", "required": required, "properties": props},
    }


def _offers(provider: VideoProvider, capability: str | None) -> bool:
    return capability is None or bool(getattr(provider.capabilities, capability, False))


def _closest(value: int, allowed: tuple[int, ...]) -> int:
    return min(allowed, key=lambda d: (abs(d - value), d))


class GenerationTools:
    """Handlers, the parallel gate and the registry sync (see the module notes)."""

    def __init__(
        self,
        providers: MediaProviders | None = None,
        *,
        resolver: Resolver | None = None,
        inner_factory: TransportFactory | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.providers = providers if providers is not None else default_providers()
        self.resolver = resolver
        self.inner_factory = inner_factory
        self.cwd = cwd
        self._gates: dict[tuple[int, str, int], asyncio.Semaphore] = {}
        self._lock = threading.Lock()
        self._registries: weakref.WeakSet[ToolRegistry] = weakref.WeakSet()
        self.providers.video_providers.add_listener(self._resync)

    # -- registration ----------------------------------------------------------------------------

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "image_generate",
            IMAGE_TOOLSET,
            {"name": "image_generate", **_image_schema(None)},
            self.image_generate,
            probe=self._image_ready,
            schema_overrides=self._image_overrides,
            # Each call only creates a new file in the media cache, so calls may run side by side
            # (the parallel gate bounds them).
            traits=ToolTraits(read_only=True),
        )
        self._registries.add(registry)
        self.sync(registry)

    def _resync(self) -> None:
        for registry in list(self._registries):
            self.sync(registry)

    def sync(self, registry: ToolRegistry) -> None:
        """Register each video tool some provider can serve; drop the others."""
        providers = self.providers.video_providers.all()
        for name, (_operation, capability) in VIDEO_TOOLS.items():
            if any(_offers(p, capability) for p in providers):
                if registry.get(name) is None:
                    registry.register(
                        name,
                        VIDEO_TOOLSET,
                        {"name": name, **_video_schema(name, None)},
                        self._video_handler(name),
                        probe=self._video_probe(name),
                        schema_overrides=self._video_overrides(name),
                    )
            elif registry.get(name) is not None:
                registry.deregister(name)

    def _image_ready(self, scope: Scope) -> bool:
        try:
            return active(self.providers.image_providers, scope, IMAGE_KEY).missing(scope) is None
        except NoProvider:
            return False

    def _image_overrides(self, scope: Scope | None) -> Mapping[str, Any]:
        try:
            provider: ImageProvider | None = active(
                self.providers.image_providers, scope, IMAGE_KEY
            )
        except NoProvider:
            provider = None
        return _image_schema(provider)

    def _video_probe(self, name: str) -> Callable[[Scope], bool]:
        capability = VIDEO_TOOLS[name][1]

        def probe(scope: Scope) -> bool:
            try:
                provider = active(self.providers.video_providers, scope, VIDEO_KEY)
            except NoProvider:
                return False
            return _offers(provider, capability) and provider.missing(scope) is None

        probe.__qualname__ = f"GenerationTools.{name}_ready"
        return probe

    def _video_overrides(self, name: str) -> Callable[[Scope | None], Mapping[str, Any]]:
        def overrides(scope: Scope | None) -> Mapping[str, Any]:
            try:
                provider: VideoProvider | None = active(
                    self.providers.video_providers, scope, VIDEO_KEY
                )
            except NoProvider:
                provider = None
            return _video_schema(name, provider)

        return overrides

    def _video_handler(self, name: str) -> Callable[..., Any]:
        operation = VIDEO_TOOLS[name][0]

        async def handler(args: Mapping[str, Any], *, scope: Scope | None = None) -> str:
            return await self.video(operation, args, scope=scope)

        handler.__qualname__ = f"GenerationTools.{name}"
        return handler

    # -- shared steps ----------------------------------------------------------------------------

    def _gate(self, scope: Scope | None) -> asyncio.Semaphore:
        configured = setting_number(scope, "image_gen.max_parallel_requests", DEFAULT_PARALLEL)
        limit = max(1, int(configured))
        key = (id(asyncio.get_running_loop()), scope_key(scope) if scope else "", limit)
        with self._lock:
            gate = self._gates.get(key)
            if gate is None:
                if len(self._gates) >= MAX_GATES:
                    self._gates.clear()
                gate = self._gates[key] = asyncio.Semaphore(limit)
            return gate

    async def _image_input(self, src: str, scope: Scope | None) -> InputMedia:
        loaded = await load_source(
            src,
            scope,
            max_bytes=MAX_INPUT_BYTES,
            cwd=self.cwd,
            resolver=self.resolver,
            inner_factory=self.inner_factory,
        )
        info = imaging.inspect(loaded.data)
        return InputMedia(data=loaded.data, mime=info.mime)

    async def _video_input(self, src: str, scope: Scope | None) -> InputMedia:
        text = src.strip()
        if text.lower().startswith(("http://", "https://")):
            verdict = await check_url_async(text, scope, resolver=self.resolver)
            if isinstance(verdict, Blocked):
                raise SourceError(f"the address was refused: {verdict.reason}")
            return InputMedia(url=text)
        loaded = await load_source(
            text, scope, max_bytes=MAX_VIDEO_INPUT_BYTES, cwd=self.cwd, resolver=self.resolver
        )
        return InputMedia(data=loaded.data, mime="video/mp4")

    async def _save(
        self, result: MediaResult, scope: Scope | None, kind: str
    ) -> tuple[str, str | None]:
        """(what to show: path or URL, sandbox path or None)."""
        if scope is None:
            if result.url:
                return result.url, None
            raise ProviderFailed("no profile is active, so the result cannot be saved")
        if result.data is not None:
            ext = result.extension or _sniff_ext(result.data, kind)
            target = folder(scope, MEDIA_DIR) / unique_name(kind, ext, scope)
            await run_blocking(atomic_write_bytes, target, result.data, label="media save")
        elif result.path is not None:
            source = Path(result.path)
            if not source.is_file():
                raise ProviderFailed(f"the provider named a file that does not exist: {source}")
            media = folder(scope, MEDIA_DIR)
            if source.resolve().parent == media.resolve():
                target = source.resolve()
            else:
                target = media / unique_name(kind, source.suffix or "bin", scope)
                await run_blocking(shutil.copyfile, source, target, label="media save")
        elif result.url:
            return result.url, None
        else:
            raise ProviderFailed("the provider returned no URL, file or data")
        return str(target), sandbox_path(scope, target)

    # -- handlers --------------------------------------------------------------------------------

    async def image_generate(self, args: Mapping[str, Any], *, scope: Scope | None = None) -> str:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return error_text("prompt is required", error_type="bad_arguments")
        try:
            provider = active(self.providers.image_providers, scope, IMAGE_KEY)
        except NoProvider as exc:
            return error_text(str(exc), error_type="no_provider")
        reason = provider.missing(scope)
        if reason:
            return error_text(
                f"image provider {provider.name!r} is not ready: {reason}", error_type="not_ready"
            )
        caps = provider.capabilities
        ratio = str(args.get("aspect_ratio") or "").strip().lower()
        if ratio not in caps.aspect_ratios:
            ratio = caps.aspect_ratios[0]
        image_src = str(args.get("image_url") or "").strip()
        refs_raw = args.get("reference_image_urls") or []
        refs = [refs_raw] if isinstance(refs_raw, str) else [str(r) for r in refs_raw if r]
        if image_src and not caps.edit:
            return error_text(
                f"{provider.title} cannot change an existing image; leave out image_url",
                error_type="unsupported",
            )
        if len(refs) > caps.max_reference_images:
            return error_text(
                f"{provider.title} takes at most {caps.max_reference_images} reference images",
                error_type="unsupported",
            )
        try:
            image = await self._image_input(image_src, scope) if image_src else None
            references = tuple([await self._image_input(r, scope) for r in refs])
        except (SourceError, imaging.BadImage) as exc:
            return error_text(f"an input image could not be used: {exc}", error_type="bad_input")
        model = setting_text(scope, "image_gen.model") or provider.default_model
        request = ImageRequest(prompt, ratio, model, image, references)
        async with self._gate(scope):
            outcome = await self._call(provider, request, scope)
        if isinstance(outcome, str):
            return outcome
        try:
            shown, inside = await self._save(outcome, scope, "image")
        except (ProviderFailed, OSError) as exc:
            return error_text(f"the image could not be saved: {exc}", error_type="save_failed")
        body: dict[str, Any] = {
            "success": True,
            "image": shown,
            "provider": provider.name,
            "model": model,
        }
        if inside:
            body["sandbox_path"] = inside
        return json_text(body)

    async def _call(
        self, provider: ImageProvider | VideoProvider, request: Any, scope: Scope | None
    ) -> MediaResult | str:
        try:
            result = await provider.generate(request, scope=scope)
        except ProviderFailed as exc:
            return error_text(f"{provider.title}: {exc}", error_type="provider_error")
        except Exception as exc:
            log.warning("media provider %s raised", provider.name, exc_info=True)
            return error_text(
                f"{provider.title} failed: {type(exc).__name__}: {exc}",
                error_type="provider_error",
            )
        if not isinstance(result, MediaResult):
            return error_text(
                f"{provider.title} returned {type(result).__name__}, not a media result",
                error_type="provider_contract",
            )
        return result

    async def video(
        self, operation: Operation, args: Mapping[str, Any], *, scope: Scope | None = None
    ) -> str:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return error_text("prompt is required", error_type="bad_arguments")
        try:
            provider = active(self.providers.video_providers, scope, VIDEO_KEY)
        except NoProvider as exc:
            return error_text(str(exc), error_type="no_provider")
        caps = provider.capabilities
        capability = {"generate": None, "edit": "edit", "extend": "extend"}[operation]
        if not _offers(provider, capability):
            return error_text(
                f"{provider.title} cannot {operation} videos", error_type="unsupported"
            )
        reason = provider.missing(scope)
        if reason:
            return error_text(
                f"video provider {provider.name!r} is not ready: {reason}", error_type="not_ready"
            )
        try:
            image = video = None
            if operation == "generate":
                src = str(args.get("image_url") or "").strip()
                if src and not caps.image_input:
                    return error_text(
                        f"{provider.title} cannot animate an image; leave out image_url",
                        error_type="unsupported",
                    )
                image = await self._image_input(src, scope) if src else None
            else:
                src = str(args.get("video_url") or "").strip()
                if not src:
                    return error_text("video_url is required", error_type="bad_arguments")
                video = await self._video_input(src, scope)
        except (SourceError, imaging.BadImage) as exc:
            return error_text(f"an input could not be used: {exc}", error_type="bad_input")
        ratio = str(args.get("aspect_ratio") or "").strip()
        resolution = str(args.get("resolution") or "").strip()
        duration: int | None = None
        raw_duration = args.get("duration")
        if isinstance(raw_duration, int | float) and not isinstance(raw_duration, bool):
            duration = int(raw_duration)
        if operation == "generate":
            ratio = ratio if ratio in caps.aspect_ratios else caps.aspect_ratios[0]
            resolution = resolution if resolution in caps.resolutions else caps.resolutions[0]
            if caps.durations:
                duration = _closest(duration, caps.durations) if duration else caps.durations[0]
            else:
                duration = None
        model = setting_text(scope, "media.video_model") or provider.default_model
        request = VideoRequest(
            prompt, model, operation, ratio, duration, resolution, image=image, video=video
        )
        outcome = await self._call(provider, request, scope)
        if isinstance(outcome, str):
            return outcome
        try:
            shown, inside = await self._save(outcome, scope, "video")
        except (ProviderFailed, OSError) as exc:
            return error_text(f"the video could not be saved: {exc}", error_type="save_failed")
        body: dict[str, Any] = {
            "success": True,
            "video": shown,
            "provider": provider.name,
            "model": model,
        }
        if inside:
            body["sandbox_path"] = inside
        return json_text(body)


def _sniff_ext(data: bytes, kind: str) -> str:
    mime = imaging.sniff(data)
    if mime is not None:
        return {"image/jpeg": "jpg"}.get(mime, mime.split("/")[1])
    if kind == "video":
        return "webm" if data[:4] == b"\x1a\x45\xdf\xa3" else "mp4"
    return "bin"


DEFAULT_TOOLS = GenerationTools()
