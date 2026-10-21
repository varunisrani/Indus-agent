"""Desktop pets (P24-19): the per-profile pet store and the two drawing steps.

A pet is made by the person, never downloaded (D-16.23):

1. ``generate_drafts`` asks the profile's image service (``image_gen.*``, P10-16) for up to four
   looks of an idea, with our own prompt. The drafts sit in ``<home>/cache/pet-drafts/<token>/``
   for ``DRAFT_TTL_S`` and are swept after that.
2. ``hatch`` turns one draft into a pet: one picture per mood (``MOODS``), each drawn by the same
   service with the draft as its reference, written to ``<home>/pets/<slug>/`` with a
   ``pet.json`` manifest. A hatched pet is not shown until ``PetStore.use`` picks it.

The whole of each step runs under its own deadline (``GENERATE_TIMEOUT_S``,
``HATCH_TIMEOUT_S``); past it the provider calls are cancelled and the half-written folder is
removed. Settings: ``pet.enabled``, ``pet.active`` (a slug) and ``pet.scale``.

Only image bytes are accepted from a provider (PNG, JPEG or WebP, at most ``MAX_IMAGE_BYTES``):
a provider that answers with a link is refused rather than fetched.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation import config
from indus.foundation.fsio import atomic_write_bytes, atomic_write_json
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.protocol.pets import (
    GENERATE_TIMEOUT_S,
    HATCH_TIMEOUT_S,
    MAX_DRAFTS,
    MAX_IDEA_CHARS,
    MAX_NAME_CHARS,
)
from indus.tools.builtin.media.providers import (
    ImageProvider,
    ImageRequest,
    InputMedia,
    MediaResult,
    NoProvider,
    active,
    default_providers,
)

__all__ = [
    "DRAFT_TTL_S",
    "GENERATE_TIMEOUT_S",
    "HATCH_TIMEOUT_S",
    "MAX_DRAFTS",
    "MAX_IMAGE_BYTES",
    "MOODS",
    "SCALE_MAX",
    "SCALE_MIN",
    "Draft",
    "DraftSet",
    "Pet",
    "PetError",
    "PetNotFound",
    "PetSettings",
    "PetStore",
    "PetTimeout",
    "PetUnavailable",
    "clamp_scale",
    "draft_prompt",
    "drafts_root",
    "generate_drafts",
    "hatch",
    "image_provider",
    "mood_prompt",
    "pets_root",
    "sniff_image",
    "slugify",
    "sprite_payload",
]

DRAFT_TTL_S = 3_600.0
"""Drafts older than this are swept at the next generate."""
MAX_IMAGE_BYTES = 16 * 1024 * 1024
SCALE_MIN = 0.25
SCALE_MAX = 3.0
MANIFEST = "pet.json"

MOODS: tuple[str, ...] = ("rest", "walk", "talk", "think", "sleep", "cheer")
"""The moods a pet is drawn in; the renderers pick one per moment (idle, moving, speaking…)."""

_MOOD_ACTION: Mapping[str, str] = {
    "rest": "sitting calmly, eyes open, relaxed",
    "walk": "mid-step, walking to the right",
    "talk": "mouth open as if saying something friendly",
    "think": "looking up thoughtfully, one paw or hand raised",
    "sleep": "curled up asleep with eyes closed",
    "cheer": "jumping with joy, arms or paws up",
}

_DRAFT_STYLES: tuple[str, ...] = (
    "soft rounded shapes, flat colours",
    "chunky pixel-art look, limited palette",
    "simple ink outline with light watercolour fill",
    "smooth vector style with a gentle gradient",
)

_TOKEN = re.compile(r"^[0-9a-f]{16}$")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class PetError(Exception):
    """A pet action that cannot be done; the message is meant for the person."""


class PetNotFound(PetError):
    """No pet (or no draft) has this name."""


class PetUnavailable(PetError):
    """No image service can draw here (not configured, a key missing)."""


class PetTimeout(PetError):
    """The image service did not finish within the step's deadline."""


# -- names, images --------------------------------------------------------------------------------


def slugify(name: str) -> str:
    """``"Otto the Otter!"`` → ``"otto-the-otter"`` (empty when nothing usable is left)."""
    return _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")[:MAX_NAME_CHARS]


def clamp_scale(value: float) -> float:
    return max(SCALE_MIN, min(SCALE_MAX, float(value)))


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """``(mime, extension)`` for PNG, JPEG and WebP bytes, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def pets_root(scope: Scope) -> Path:
    return scope.home / "pets"


def drafts_root(scope: Scope) -> Path:
    return scope.home / "cache" / "pet-drafts"


def draft_prompt(idea: str, variant: int) -> str:
    """The prompt for one draft look of ``idea`` (our own wording)."""
    style = _DRAFT_STYLES[variant % len(_DRAFT_STYLES)]
    return (
        f"A small friendly desktop companion character: {idea}. Full body, facing the viewer, "
        f"centred, on a plain transparent or white background, no text, no frame. Style: {style}."
    )


def mood_prompt(idea: str, mood: str) -> str:
    """The prompt for one mood of a hatched pet, drawn after the reference picture."""
    action = _MOOD_ACTION.get(mood, mood)
    return (
        f"The same character as in the reference picture ({idea}), {action}. Keep its colours, "
        "shape and style exactly; full body, centred, plain transparent or white background, "
        "no text."
    )


# -- the store ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pet:
    slug: str
    name: str
    idea: str
    created_at: float
    moods: Mapping[str, str]
    """Mood → picture file name inside ``directory``."""
    directory: Path

    def picture(self, mood: str) -> Path | None:
        name = self.moods.get(mood)
        return self.directory / name if name else None


@dataclass(frozen=True)
class PetSettings:
    enabled: bool
    active: str
    scale: float


def _settings_of(scope: Scope) -> PetSettings:
    view = config.load(scope)
    raw_scale: object = view.get("pet.scale", 1.0)
    try:
        scale = clamp_scale(float(cast(Any, raw_scale)))
    except (TypeError, ValueError):
        scale = 1.0
    return PetSettings(
        enabled=view.get("pet.enabled", False) is True,
        active=str(view.get("pet.active", "") or ""),
        scale=scale,
    )


def _read_manifest(directory: Path) -> Pet | None:
    try:
        raw: object = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, Any], raw)
    moods_raw: object = data.get("moods")
    moods: dict[str, str] = {}
    if isinstance(moods_raw, dict):
        for mood, file_name in cast(dict[object, object], moods_raw).items():
            if (
                isinstance(mood, str)
                and isinstance(file_name, str)
                and "/" not in file_name
                and "\\" not in file_name
                and (directory / file_name).is_file()
            ):
                moods[mood] = file_name
    if not moods:
        return None
    created: object = data.get("created_at")
    return Pet(
        slug=directory.name,
        name=str(data.get("name") or directory.name),
        idea=str(data.get("idea") or ""),
        created_at=float(created) if isinstance(created, (int, float)) else 0.0,
        moods=moods,
        directory=directory,
    )


class PetStore:
    """The pets of one profile and the settings that choose the shown one."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    @property
    def root(self) -> Path:
        return pets_root(self.scope)

    def list(self) -> list[Pet]:
        if not self.root.is_dir():
            return []
        found = [_read_manifest(child) for child in sorted(self.root.iterdir()) if child.is_dir()]
        return [pet for pet in found if pet is not None]

    def get(self, slug: str) -> Pet:
        clean = slugify(slug)
        pet = _read_manifest(self.root / clean) if clean else None
        if pet is None:
            raise PetNotFound(f"No pet named {slug!r}. `indus pet list` shows them.")
        return pet

    def settings(self) -> PetSettings:
        return _settings_of(self.scope)

    def shown(self) -> Pet | None:
        """The pet the surfaces show now, or None when the display is off or the slug is gone."""
        current = self.settings()
        if not current.enabled or not current.active:
            return None
        try:
            return self.get(current.active)
        except PetNotFound:
            return None

    def unique_slug(self, name: str) -> str:
        base = slugify(name)
        if not base:
            raise PetError("The name needs at least one letter or digit.")
        slug, n = base, 2
        while (self.root / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        return slug

    def install(
        self, name: str, idea: str, pictures: Mapping[str, tuple[bytes, str]], *, now: float
    ) -> Pet:
        """Write a new pet (``pictures``: mood → (bytes, extension)); it is not made active."""
        clean_name = " ".join(name.split())[:MAX_NAME_CHARS]
        slug = self.unique_slug(clean_name)
        secure_dir(self.root, home=self.scope.home)
        staging = self.root / f".{slug}.tmp-{secrets.token_hex(4)}"
        staging.mkdir(mode=0o700)
        try:
            moods: dict[str, str] = {}
            for mood, (data, ext) in pictures.items():
                file_name = f"{mood}.{ext}"
                atomic_write_bytes(staging / file_name, data)
                moods[mood] = file_name
            atomic_write_json(
                staging / MANIFEST,
                {"name": clean_name, "idea": idea, "created_at": now, "moods": moods},
            )
            staging.rename(self.root / slug)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return self.get(slug)

    def use(self, slug: str) -> Pet:
        pet = self.get(slug)
        config.save(self.scope, {"pet": {"active": pet.slug, "enabled": True}})
        return pet

    def off(self) -> None:
        config.save(self.scope, {"pet": {"enabled": False}})

    def set_scale(self, value: float) -> float:
        scale = clamp_scale(value)
        config.save(self.scope, {"pet": {"scale": scale}})
        return scale

    def rename(self, slug: str, new_name: str) -> Pet:
        """Give a pet a new name (its folder and slug follow; the active setting follows too)."""
        pet = self.get(slug)
        clean_name = " ".join(new_name.split())[:MAX_NAME_CHARS]
        target = slugify(clean_name)
        if not target:
            raise PetError("The name needs at least one letter or digit.")
        if target != pet.slug:
            target = self.unique_slug(clean_name)
            pet.directory.rename(self.root / target)
        manifest_path = self.root / target / MANIFEST
        raw = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
        raw["name"] = clean_name
        atomic_write_json(manifest_path, raw)
        if self.settings().active == pet.slug and target != pet.slug:
            config.save(self.scope, {"pet": {"active": target}})
        return self.get(target)

    def remove(self, slug: str) -> Pet:
        """Delete a pet; when it was the shown one, the display turns off."""
        pet = self.get(slug)
        shutil.rmtree(pet.directory)
        if self.settings().active == pet.slug:
            config.save(self.scope, {"pet": {"active": "", "enabled": False}})
        return pet


# -- drawing --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    index: int
    path: Path
    mime: str


@dataclass(frozen=True)
class DraftSet:
    token: str
    idea: str
    drafts: tuple[Draft, ...]
    directory: Path


type ProviderFor = Callable[[Scope], ImageProvider]


def image_provider(scope: Scope) -> ImageProvider:
    """The profile's image service (``image_gen.provider``, else the first ready one)."""
    try:
        provider = active(default_providers().image_providers, scope, "image_gen.provider")
    except NoProvider as exc:
        raise PetUnavailable(f"No image service can draw the pet: {exc}.") from None
    reason = provider.missing(scope)
    if reason:
        raise PetUnavailable(f"The image service {provider.name} is not ready: {reason}.")
    return provider


def _configured(scope: Scope) -> Scope:
    return scope.with_config(config.load(scope))


def _bytes_of(result: MediaResult) -> bytes:
    data = result.data
    if data is None and result.path is not None:
        size = result.path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise PetError("the image service sent a picture larger than 16 MiB")
        data = result.path.read_bytes()
    if data is None:
        raise PetError("the image service answered with a link instead of a picture")
    if len(data) > MAX_IMAGE_BYTES:
        raise PetError("the image service sent a picture larger than 16 MiB")
    return data


def _picture(result: MediaResult) -> tuple[bytes, str, str]:
    data = _bytes_of(result)
    kind = sniff_image(data)
    if kind is None:
        raise PetError("the image service sent something that is not a PNG, JPEG or WebP picture")
    return data, kind[0], kind[1]


def _sweep(root: Path, now: float) -> None:
    if not root.is_dir():
        return
    for child in root.iterdir():
        try:
            if child.is_dir() and now - child.stat().st_mtime > DRAFT_TTL_S:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _model(scope: Scope, provider: ImageProvider) -> str:
    value: object = config.load(scope).get("image_gen.model", "")
    text = value.strip() if isinstance(value, str) else ""
    return text or provider.default_model


async def generate_drafts(
    scope: Scope,
    idea: str,
    *,
    count: int = MAX_DRAFTS,
    provider: ImageProvider | None = None,
    timeout_s: float = GENERATE_TIMEOUT_S,
    now: Callable[[], float] = time.time,
) -> DraftSet:
    """Draw up to ``count`` looks of ``idea`` (see the module notes)."""
    text = " ".join(idea.split())[:MAX_IDEA_CHARS]
    if not text:
        raise PetError("Describe the pet first, for example: a small sleepy otter.")
    wanted = max(1, min(MAX_DRAFTS, int(count)))
    live = _configured(scope)
    chosen = provider if provider is not None else image_provider(live)
    root = drafts_root(scope)
    secure_dir(root, home=scope.home)
    _sweep(root, now())
    token = secrets.token_hex(8)
    folder = root / token
    folder.mkdir(mode=0o700)
    model = _model(scope, chosen)

    async def one(index: int) -> Draft:
        request = ImageRequest(prompt=draft_prompt(text, index), aspect_ratio="square", model=model)
        data, mime, ext = _picture(await chosen.generate(request, scope=live))
        path = atomic_write_bytes(folder / f"draft-{index}.{ext}", data)
        return Draft(index=index, path=path, mime=mime)

    try:
        async with asyncio.timeout(timeout_s):
            outcomes = await asyncio.gather(
                *(one(i) for i in range(wanted)), return_exceptions=True
            )
    except TimeoutError:
        shutil.rmtree(folder, ignore_errors=True)
        raise PetTimeout(
            f"The image service did not finish the drafts within {timeout_s:g} s."
        ) from None
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    drafts = tuple(o for o in outcomes if isinstance(o, Draft))
    if not drafts:
        shutil.rmtree(folder, ignore_errors=True)
        first = next((o for o in outcomes if isinstance(o, BaseException)), None)
        raise PetError(f"No usable draft came back: {first}")
    atomic_write_bytes(folder / "idea.txt", text.encode("utf-8"))
    return DraftSet(token=token, idea=text, drafts=drafts, directory=folder)


def _draft_file(scope: Scope, token: str, pick: int) -> Path:
    if not _TOKEN.match(token):
        raise PetNotFound("That draft token is not one `indus pet generate` printed.")
    folder = drafts_root(scope) / token
    for ext in ("png", "jpg", "webp"):
        candidate = folder / f"draft-{pick}.{ext}"
        if candidate.is_file():
            return candidate
    if not folder.is_dir():
        raise PetNotFound("Those drafts are gone (they are kept for an hour); generate again.")
    raise PetNotFound(f"There is no draft {pick} in that set.")


async def hatch(
    scope: Scope,
    token: str,
    pick: int,
    name: str,
    *,
    provider: ImageProvider | None = None,
    timeout_s: float = HATCH_TIMEOUT_S,
    now: Callable[[], float] = time.time,
    moods: Sequence[str] = MOODS,
) -> Pet:
    """Draw every mood after draft ``pick`` of ``token`` and install the pet (not active)."""
    store = PetStore(scope)
    store.unique_slug(name)  # a name that slugs to nothing fails before any drawing
    draft = _draft_file(scope, token, pick)
    reference = draft.read_bytes()
    kind = sniff_image(reference)
    mime = kind[0] if kind is not None else "image/png"
    try:
        idea = (draft.parent / "idea.txt").read_text(encoding="utf-8").strip()
    except OSError:
        idea = ""
    idea = idea or " ".join(name.split())
    live = _configured(scope)
    chosen = provider if provider is not None else image_provider(live)
    model = _model(scope, chosen)
    given = InputMedia(data=reference, mime=mime)
    caps = chosen.capabilities
    pictures: dict[str, tuple[bytes, str]] = {}

    async def draw_all() -> None:
        for mood in moods:
            request = ImageRequest(
                prompt=mood_prompt(idea, mood),
                aspect_ratio="square",
                model=model,
                image=given if caps.edit else None,
                references=(given,) if not caps.edit and caps.max_reference_images > 0 else (),
            )
            data, _mime, ext = _picture(await chosen.generate(request, scope=live))
            pictures[mood] = (data, ext)

    try:
        async with asyncio.timeout(timeout_s):
            await draw_all()
    except TimeoutError:
        raise PetTimeout(
            f"The image service did not finish the pet within {timeout_s:g} s."
        ) from None
    return store.install(name, idea, pictures, now=now())


def sprite_payload(pet: Pet, settings: PetSettings) -> dict[str, Any]:
    """What a renderer needs to show ``pet``: name, scale, a revision and one data URI per mood."""
    digest = hashlib.sha256()
    moods: dict[str, str] = {}
    for mood in sorted(pet.moods):
        path = pet.picture(mood)
        if path is None:
            continue
        data = path.read_bytes()
        digest.update(mood.encode() + b"\0" + data)
        kind = sniff_image(data)
        mime = kind[0] if kind is not None else "application/octet-stream"
        moods[mood] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    digest.update(pet.name.encode())
    return {
        "slug": pet.slug,
        "name": pet.name,
        "scale": settings.scale,
        "revision": digest.hexdigest()[:16],
        "moods": moods,
    }
