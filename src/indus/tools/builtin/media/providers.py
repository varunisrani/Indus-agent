"""Image and video generation providers and their registries (P10-16).

A provider has a ``name``, a ``title``, a ``default_model``, declared ``capabilities`` and
``generate(request, scope=…) -> MediaResult``; ``missing(scope)`` says why it cannot run here
(usually a key that is not set) or None. ``MediaResult`` carries a URL, a file path or raw bytes;
the tools save bytes and paths under ``<home>/cache/media/``.

``MediaProviders`` holds the two registries (``image_providers``, ``video_providers``). Plugins
(P13) add providers there; listeners hear every change, which is how the video tools appear when
a provider declaring video capabilities registers. ``active(registry, scope, key)`` picks the
provider the user configured (``image_gen.provider`` / ``media.video_provider``), else the first
ready one, else the first registered (so its "not ready" reason reaches the model).

The one built-in provider, ``OpenAIImages``, speaks the OpenAI-compatible images API: JSON
``POST {base}/images/generations`` for text-to-image and multipart ``POST {base}/images/edits``
when input images are given; the answer is ``data[0].b64_json`` or ``data[0].url``. Its key is
read from the secret store (``image_gen.key_name``, default ``OPENAI_API_KEY``).
"""

from __future__ import annotations

import base64
import binascii
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

import httpx

from indus.foundation.scope import Scope
from indus.safety.url_policy import proxy_for
from indus.tools.builtin.media.common import secret, setting_text

__all__ = [
    "IMAGE_ASPECT_RATIOS",
    "VIDEO_ASPECT_RATIOS",
    "ImageCapabilities",
    "ImageProvider",
    "ImageRequest",
    "InputMedia",
    "MediaProviders",
    "MediaResult",
    "NoProvider",
    "OpenAIImages",
    "ProviderFailed",
    "ProviderRegistry",
    "Ready",
    "VideoCapabilities",
    "VideoProvider",
    "VideoRequest",
    "active",
    "default_providers",
]

log = logging.getLogger(__name__)

IMAGE_ASPECT_RATIOS: tuple[str, ...] = ("landscape", "square", "portrait")
VIDEO_ASPECT_RATIOS: tuple[str, ...] = ("16:9", "9:16", "1:1")
IMAGE_TIMEOUT_S = 180.0
MAX_ERROR_DETAIL = 400


class ProviderFailed(RuntimeError):
    """A provider could not produce the media; the message is meant for the model."""


class NoProvider(LookupError):
    """No usable provider for this profile; the message says what to configure."""


@dataclass(frozen=True)
class ImageCapabilities:
    aspect_ratios: tuple[str, ...] = IMAGE_ASPECT_RATIOS
    edit: bool = False  # changes an input image (``image_url``)
    max_reference_images: int = 0


@dataclass(frozen=True)
class VideoCapabilities:
    aspect_ratios: tuple[str, ...] = VIDEO_ASPECT_RATIOS
    durations: tuple[int, ...] = ()  # seconds offered; empty = the provider's default only
    resolutions: tuple[str, ...] = ("720p",)
    image_input: bool = False  # animates a still image (``image_url``)
    edit: bool = False  # ``video_edit``
    extend: bool = False  # ``video_extend``


@dataclass(frozen=True)
class InputMedia:
    """An input the tool already loaded and checked: bytes, or a URL the policy allowed."""

    data: bytes | None = None
    url: str | None = None
    mime: str = ""


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    aspect_ratio: str
    model: str
    image: InputMedia | None = None
    references: tuple[InputMedia, ...] = ()


@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    model: str
    operation: Literal["generate", "edit", "extend"] = "generate"
    aspect_ratio: str = ""
    duration: int | None = None
    resolution: str = ""
    image: InputMedia | None = None
    video: InputMedia | None = None


@dataclass(frozen=True)
class MediaResult:
    url: str | None = None
    path: Path | None = None
    data: bytes | None = None
    extension: str = ""  # for ``data``; sniffed when empty
    extra: Mapping[str, Any] = field(default_factory=dict[str, Any])


@runtime_checkable
class ImageProvider(Protocol):
    name: str
    title: str
    default_model: str
    capabilities: ImageCapabilities

    def missing(self, scope: Scope | None) -> str | None: ...

    async def generate(self, request: ImageRequest, *, scope: Scope | None) -> MediaResult: ...


@runtime_checkable
class VideoProvider(Protocol):
    name: str
    title: str
    default_model: str
    capabilities: VideoCapabilities

    def missing(self, scope: Scope | None) -> str | None: ...

    async def generate(self, request: VideoRequest, *, scope: Scope | None) -> MediaResult: ...


class Ready(Protocol):
    """What every registry entry has: a name and a readiness answer."""

    @property
    def name(self) -> str: ...

    def missing(self, scope: Scope | None) -> str | None: ...


class ProviderRegistry[P: Ready]:
    """Providers by name, in registration order, with change listeners."""

    def __init__(self, kind: str, providers: Iterable[P] = ()) -> None:
        self.kind = kind
        self._providers: dict[str, P] = {}
        self._listeners: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        for provider in providers:
            self.register(provider)

    def register(self, provider: P, *, replace: bool = False) -> None:
        key = provider.name.strip().lower()
        with self._lock:
            if key in self._providers and not replace:
                raise ValueError(f"{self.kind} provider {provider.name!r} is already registered")
            self._providers[key] = provider
        self._changed()

    def unregister(self, name: str) -> bool:
        with self._lock:
            removed = self._providers.pop(name.strip().lower(), None) is not None
        if removed:
            self._changed()
        return removed

    def get(self, name: str) -> P | None:
        return self._providers.get(name.strip().lower())

    def names(self) -> list[str]:
        return list(self._providers)

    def all(self) -> list[P]:
        return list(self._providers.values())

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.warning("a %s provider listener failed", self.kind, exc_info=True)


@dataclass
class MediaProviders:
    image_providers: ProviderRegistry[ImageProvider]
    video_providers: ProviderRegistry[VideoProvider]


def active[P: Ready](registry: ProviderRegistry[P], scope: Scope | None, config_key: str) -> P:
    """The provider for ``scope`` (see the module notes); ``NoProvider`` when there is none."""
    configured = setting_text(scope, config_key).lower()
    known = ", ".join(registry.names()) or "none"
    if configured:
        chosen = registry.get(configured)
        if chosen is None:
            raise NoProvider(
                f"{config_key} is {configured!r}, which is not a registered {registry.kind} "
                f"provider (registered: {known})"
            )
        return chosen
    providers = registry.all()
    if not providers:
        raise NoProvider(f"no {registry.kind} provider is registered")
    for provider in providers:
        try:
            if provider.missing(scope) is None:
                return provider
        except Exception:
            log.debug("%s provider %s readiness check failed", registry.kind, provider.name)
    return providers[0]


# -- the built-in image provider ----------------------------------------------------------------

DEFAULT_IMAGE_BASE_URL = "https://api.openai.com/v1"
DEFAULT_IMAGE_KEY = "OPENAI_API_KEY"
_SIZES = {"landscape": "1536x1024", "square": "1024x1024", "portrait": "1024x1536"}

type ClientFactory = Callable[[Scope | None, float], httpx.AsyncClient]


def default_client(scope: Scope | None, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout, trust_env=False, proxy=proxy_for(scope, "https") if scope else None
    )


def _detail(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:MAX_ERROR_DETAIL]
    if isinstance(body, dict):
        error = cast(dict[str, Any], body).get("error")
        if isinstance(error, dict):
            message = cast(dict[str, Any], error).get("message")
            if isinstance(message, str):
                return message[:MAX_ERROR_DETAIL]
        if isinstance(error, str):
            return error[:MAX_ERROR_DETAIL]
    return response.text[:MAX_ERROR_DETAIL]


class OpenAIImages:
    """Any OpenAI-compatible images endpoint (see the module notes)."""

    name: str = "openai"
    title: str = "OpenAI-compatible images API"
    default_model: str = "gpt-image-1"
    capabilities: ImageCapabilities = ImageCapabilities(edit=True, max_reference_images=4)

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self.client_factory = client_factory or default_client

    @staticmethod
    def key_name(scope: Scope | None) -> str:
        return setting_text(scope, "image_gen.key_name", DEFAULT_IMAGE_KEY) or DEFAULT_IMAGE_KEY

    def missing(self, scope: Scope | None) -> str | None:
        name = self.key_name(scope)
        return None if secret(scope, name) else f"{name} is not set"

    @staticmethod
    def base_url(scope: Scope | None) -> str:
        return (setting_text(scope, "image_gen.base_url") or DEFAULT_IMAGE_BASE_URL).rstrip("/")

    async def generate(self, request: ImageRequest, *, scope: Scope | None) -> MediaResult:
        key = secret(scope, self.key_name(scope))
        if not key:
            raise ProviderFailed(f"{self.key_name(scope)} is not set")
        headers = {"Authorization": f"Bearer {key}"}
        size = _SIZES.get(request.aspect_ratio, _SIZES["square"])
        inputs = [m for m in (request.image, *request.references) if m is not None]
        base = self.base_url(scope)
        async with self.client_factory(scope, IMAGE_TIMEOUT_S) as client:
            try:
                if inputs:
                    files = [
                        ("image[]", (f"input-{i}.{_ext(m.mime)}", m.data or b"", m.mime))
                        for i, m in enumerate(inputs)
                    ]
                    form = {"model": request.model, "prompt": request.prompt, "size": size}
                    response = await client.post(
                        f"{base}/images/edits", headers=headers, data=form, files=files
                    )
                else:
                    payload = {
                        "model": request.model,
                        "prompt": request.prompt,
                        "size": size,
                        "n": 1,
                    }
                    response = await client.post(
                        f"{base}/images/generations", headers=headers, json=payload
                    )
            except httpx.TimeoutException as exc:
                raise ProviderFailed("the images endpoint did not answer in time") from exc
            except httpx.HTTPError as exc:
                raise ProviderFailed(f"could not reach the images endpoint: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderFailed(
                f"the images endpoint answered HTTP {response.status_code}: {_detail(response)}"
            )
        return _image_result(response)


def _ext(mime: str) -> str:
    return {"image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")


def _image_result(response: httpx.Response) -> MediaResult:
    try:
        body: Any = response.json()
    except ValueError as exc:
        raise ProviderFailed("the images endpoint sent a reply that is not JSON") from exc
    items: Any = body.get("data") if isinstance(body, dict) else None
    first: Any = items[0] if isinstance(items, list) and items else None
    if not isinstance(first, dict):
        raise ProviderFailed("the images endpoint sent no image")
    entry = cast(dict[str, Any], first)
    encoded = entry.get("b64_json")
    if isinstance(encoded, str) and encoded:
        try:
            return MediaResult(data=base64.b64decode(encoded, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ProviderFailed("the images endpoint sent invalid base64") from exc
    url = entry.get("url")
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        return MediaResult(url=url)
    raise ProviderFailed("the images endpoint sent no image")


def default_providers() -> MediaProviders:
    return MediaProviders(
        image_providers=ProviderRegistry[ImageProvider]("image", [OpenAIImages()]),
        video_providers=ProviderRegistry[VideoProvider]("video"),
    )
