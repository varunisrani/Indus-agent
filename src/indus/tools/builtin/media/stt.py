"""Speech to text, as a library for voice mode (P10-19) and gateway voice intake (P15) (P10-18).

``await transcribe(path, scope=…)`` → ``Transcript{text, language, duration, inaudible, provider,
pieces}`` or ``SttError`` (a plain message). It is not a model tool.

Providers implement ``transcribe(path, language, scope=…) -> Transcript`` and record their limits:
``max_bytes`` per request (None = the profile's ``stt.max_file_mb``, 25 MB) and ``max_seconds``
per request (None = no limit). Built-ins, also the automatic order when ``stt.provider`` is empty:

- ``local``: faster-whisper (the ``voice`` extra), model size ``stt.local_model``;
- ``openai``: any OpenAI-compatible ``POST {base}/audio/transcriptions`` (25 MB), key
  ``OPENAI_API_KEY``, base ``stt.base_url``;
- ``groq``: Groq's endpoint with the same API shape (25 MB), key ``GROQ_API_KEY``.

A file over the provider's byte cap, or longer than its seconds cap, is cut into pieces at pauses
(``audio.silences`` / ``audio.cut_points``: latest pause within a lookback of at most 60 s before
each ideal cut) and the pieces are transcribed in order and joined. Only PCM WAV can be cut here;
anything else over the cap is refused with a plain message. An empty result never comes back as
empty text: ``inaudible`` is set and ``text`` says nothing could be heard (R06.23). Files the
``read_file`` rules protect (secret stores, ``.env``) are never uploaded.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.safety.path_guards import blocked_device, read_denial
from indus.tools.builtin.media import audio
from indus.tools.builtin.media.common import AUDIO_DIR, folder, secret, setting_number, setting_text
from indus.tools.builtin.media.providers import (
    ClientFactory,
    NoProvider,
    ProviderRegistry,
    active,
    default_client,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "INAUDIBLE_TEXT",
    "GroqTranscription",
    "LocalWhisper",
    "OpenAITranscription",
    "SttError",
    "SttProvider",
    "Transcript",
    "default_registry",
    "transcribe",
]

log = logging.getLogger(__name__)

MB = 1024 * 1024
DEFAULT_MAX_BYTES = 25 * MB
CONFIG_KEY = "stt.provider"
INAUDIBLE_TEXT = (
    "(inaudible: nothing could be made out in this recording; ask for it to be sent again)"
)
HEADROOM = 0.9  # pieces aim below the caps so container overhead never tips one over
REQUEST_TIMEOUT_S = 120.0
VOICE_EXTRA = "voice"


class SttError(RuntimeError):
    """Transcription failed; the message is meant for a person or the model."""


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str = ""
    duration: float | None = None
    inaudible: bool = False
    provider: str = ""
    pieces: int = 1


@runtime_checkable
class SttProvider(Protocol):
    name: str
    title: str
    max_bytes: int | None
    max_seconds: float | None

    def missing(self, scope: Scope | None) -> str | None: ...

    async def transcribe(
        self, path: Path, language: str | None, *, scope: Scope | None
    ) -> Transcript: ...


# -- built-in providers -------------------------------------------------------------------------


class LocalWhisper:
    """faster-whisper on this machine; the model is loaded once per (size, device) per process."""

    name: str = "local"
    title: str = "faster-whisper (local)"
    max_bytes: int | None = None
    max_seconds: float | None = None
    default_size = "base"

    def __init__(self) -> None:
        self._models: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def missing(self, scope: Scope | None) -> str | None:
        if importlib.util.find_spec("faster_whisper") is None:
            return (
                "faster-whisper is not installed (install the `voice` extra: "
                f"pip install 'indus-agent[{VOICE_EXTRA}]')"
            )
        return None

    def _model(self, size: str, device: str) -> Any:
        with self._lock:
            model = self._models.get((size, device))
            if model is None:
                module = importlib.import_module("faster_whisper")
                model = module.WhisperModel(size, device=device, compute_type="default")
                self._models[(size, device)] = model
            return model

    def _run(self, path: Path, language: str | None, size: str, device: str) -> Transcript:
        model = self._model(size, device)
        segments, info = model.transcribe(
            str(path),
            language=language or None,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(str(s.text).strip() for s in segments if str(s.text).strip())
        return Transcript(
            text,
            str(getattr(info, "language", "") or ""),
            float(getattr(info, "duration", 0.0) or 0.0) or None,
            provider=self.name,
        )

    async def transcribe(
        self, path: Path, language: str | None, *, scope: Scope | None
    ) -> Transcript:
        reason = self.missing(scope)
        if reason:
            raise SttError(reason)
        size = setting_text(scope, "stt.local_model", self.default_size) or self.default_size
        device = setting_text(scope, "stt.local_device", "auto") or "auto"
        return await run_blocking(self._run, path, language, size, device, label="local stt")


def _plain_http(status: int, title: str, detail: str) -> str:
    if status in (401, 403):
        return f"{title} refused the key (HTTP {status}); check it with `indus auth`"
    if status == 413:
        return f"{title} says the audio is too large (HTTP 413)"
    if status == 429:
        return f"{title} is rate limiting requests (HTTP 429); try again shortly"
    if status >= 500:
        return f"{title} had a server error (HTTP {status}); try again later"
    return f"{title} could not transcribe the audio (HTTP {status}): {detail}"


class OpenAITranscription:
    """Any OpenAI-compatible transcription endpoint (see the module notes)."""

    name: str = "openai"
    title: str = "OpenAI-compatible transcription API"
    max_bytes: int | None = 25 * MB
    max_seconds: float | None = None
    key: str = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com/v1"
    default_model: str = "whisper-1"
    base_url_key: str | None = "stt.base_url"

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self.client_factory = client_factory or default_client

    def missing(self, scope: Scope | None) -> str | None:
        return None if secret(scope, self.key) else f"{self.key} is not set"

    def _base(self, scope: Scope | None) -> str:
        configured = setting_text(scope, self.base_url_key) if self.base_url_key else ""
        return (configured or self.base_url).rstrip("/")

    async def transcribe(
        self, path: Path, language: str | None, *, scope: Scope | None
    ) -> Transcript:
        key = secret(scope, self.key)
        if not key:
            raise SttError(f"{self.key} is not set")
        model = setting_text(scope, "stt.model") or self.default_model
        form = {"model": model, "response_format": "verbose_json"}
        if not model.startswith("whisper"):
            form["response_format"] = "json"  # the newer models answer json/text only
        if language:
            form["language"] = language
        data = await run_blocking(path.read_bytes, label="stt read")
        files = {"file": (path.name, data, "application/octet-stream")}
        try:
            async with self.client_factory(scope, REQUEST_TIMEOUT_S) as client:
                response = await client.post(
                    f"{self._base(scope)}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    data=form,
                    files=files,
                )
        except httpx.TimeoutException as exc:
            raise SttError(f"{self.title} did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise SttError(f"could not reach {self.title}: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise SttError(_plain_http(response.status_code, self.title, response.text[:200]))
        try:
            body: Any = response.json()
        except ValueError as exc:
            raise SttError(f"{self.title} sent a reply that is not JSON") from exc
        if not isinstance(body, dict):
            raise SttError(f"{self.title} sent an unexpected reply")
        reply = cast(dict[str, Any], body)
        duration = reply.get("duration")
        return Transcript(
            str(reply.get("text") or ""),
            str(reply.get("language") or language or ""),
            float(duration) if isinstance(duration, int | float) else None,
            provider=self.name,
        )


class GroqTranscription(OpenAITranscription):
    name: str = "groq"
    title: str = "Groq transcription API"
    key: str = "GROQ_API_KEY"
    base_url: str = "https://api.groq.com/openai/v1"
    default_model: str = "whisper-large-v3-turbo"
    base_url_key: str | None = None


def default_registry() -> ProviderRegistry[SttProvider]:
    return ProviderRegistry[SttProvider](
        "transcription", [LocalWhisper(), OpenAITranscription(), GroqTranscription()]
    )


DEFAULT_REGISTRY = default_registry()


# -- the library call ---------------------------------------------------------------------------


def _choose(
    registry: ProviderRegistry[SttProvider], scope: Scope | None, name: str | None
) -> SttProvider:
    if name:
        chosen = registry.get(name)
        if chosen is None:
            known = ", ".join(registry.names()) or "none"
            raise SttError(f"{name!r} is not a transcription provider (known: {known})")
        return chosen
    try:
        return active(registry, scope, CONFIG_KEY)
    except NoProvider as exc:
        raise SttError(str(exc)) from exc


def _cap(provider: SttProvider, scope: Scope | None) -> int:
    if provider.max_bytes:
        return provider.max_bytes
    return int(setting_number(scope, "stt.max_file_mb", DEFAULT_MAX_BYTES / MB) * MB)


async def _one(
    provider: SttProvider, path: Path, language: str | None, scope: Scope | None
) -> Transcript:
    try:
        return await provider.transcribe(path, language, scope=scope)
    except SttError:
        raise
    except Exception as exc:
        log.info("transcription by %s failed", provider.name, exc_info=True)
        raise SttError(f"{provider.title} failed: {type(exc).__name__}: {exc}") from exc


def _finish(parts: list[Transcript], provider: SttProvider, duration: float | None) -> Transcript:
    texts = [p.text.strip() for p in parts if p.text.strip() and not p.inaudible]
    language = next((p.language for p in parts if p.language), "")
    if duration is None and parts and all(p.duration is not None for p in parts):
        duration = sum(p.duration or 0.0 for p in parts)
    if not texts:
        return Transcript(INAUDIBLE_TEXT, language, duration, True, provider.name, len(parts))
    return Transcript(" ".join(texts), language, duration, False, provider.name, len(parts))


async def transcribe(
    path: str | Path,
    *,
    scope: Scope | None,
    language: str | None = None,
    provider: str | None = None,
    registry: ProviderRegistry[SttProvider] | None = None,
) -> Transcript:
    """See the module notes."""
    resolved = Path(os.path.realpath(path))
    if blocked_device(str(resolved)):
        raise SttError(f"{resolved} is a device, not an audio file")
    denial = read_denial(str(resolved), scope)
    if denial:
        raise SttError(denial)
    if not resolved.is_file():
        raise SttError(f"no audio file at {resolved}")
    size = resolved.stat().st_size
    if size == 0:
        raise SttError("the audio file is empty")
    chosen = _choose(registry or DEFAULT_REGISTRY, scope, provider)
    reason = chosen.missing(scope)
    if reason:
        raise SttError(f"transcription provider {chosen.name!r} is not ready: {reason}")
    language = language or setting_text(scope, "stt.language") or None
    cap = _cap(chosen, scope)
    with resolved.open("rb") as handle:
        is_wav = audio.is_wav(handle.read(12))
    duration: float | None = None
    if is_wav:
        try:
            duration = audio.wav_facts(resolved).duration
        except Exception:
            is_wav = False  # not plain PCM WAV: treat it like any other format
    too_long = bool(chosen.max_seconds and duration and duration > chosen.max_seconds)
    if size <= cap and not too_long:
        return _finish([await _one(chosen, resolved, language, scope)], chosen, duration)
    if not is_wav or duration is None:
        raise SttError(
            f"the file is {size / MB:.1f} MB and {chosen.title} takes at most "
            f"{cap / MB:.0f} MB per request; only WAV audio can be cut into pieces here, so "
            "trim or compress the recording first"
        )
    target = duration
    if size > cap:
        target = min(target, duration * cap * HEADROOM / size)
    if chosen.max_seconds:
        target = min(target, chosen.max_seconds * HEADROOM)
    quiet = await run_blocking(audio.silences, resolved, label="stt silences")
    cuts = audio.cut_points(duration, target, quiet)
    work_root = folder(scope, AUDIO_DIR) if scope is not None else None
    with tempfile.TemporaryDirectory(prefix="stt-pieces-", dir=work_root) as work:
        pieces = await run_blocking(audio.split_wav, resolved, cuts, Path(work), label="stt split")
        parts: list[Transcript] = []
        for index, piece in enumerate(pieces, start=1):
            try:
                parts.append(await _one(chosen, piece, language, scope))
            except SttError as exc:
                raise SttError(f"piece {index} of {len(pieces)}: {exc}") from exc
    result = _finish(parts, chosen, duration)
    return replace(result, pieces=len(pieces))
