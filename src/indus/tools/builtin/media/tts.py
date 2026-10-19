"""``text_to_speech``: speak text into an audio file and hand back ``MEDIA:<path>`` (P10-17).

Providers implement ``synthesize(text, voice, speed, fmt, scope=…, instructions=…) -> bytes`` and
declare ``max_chars`` (per request), ``speed_range`` and ``formats`` (what they can emit, the
first being the one to use when nothing else is asked). Built-ins:

- ``piper`` (default, D-16.71): the local Piper engine (MIT), run as a separate program: text on
  stdin, a WAV file out, the voice model from ``tts.piper_model``. No key, no network.
- ``openai``: any OpenAI-compatible ``POST {base}/audio/speech`` (4,096 characters per request,
  speed 0.25–4.0; mp3, opus, wav, aac, flac), key from the secret store.

Flow: fold whitespace → split into ordered pieces under the provider's cap (sentence ends first,
then word boundaries, then a hard cut) → clamp the speed to 0.25–4.0 and to the provider's range →
synthesise each piece in order (every body capped at ``MAX_BODY_BYTES``) → join (MP3 frames
append; WAV pieces merge under one header) → write the file → when a voice bubble is wanted and
the audio is not Opus, convert it with ``ffmpeg`` (absent → keep MP3/WAV and say so).

Where the file goes: the caller's ``output_path`` (checked by the media path rules in
``common.checked_output_path``), else ``<home>/voice-memos/`` on the CLI and
``<home>/cache/audio/`` elsewhere.
"""

from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.fsio import atomic_write_bytes
from indus.foundation.scope import Scope
from indus.tools.builtin.media import audio
from indus.tools.builtin.media.common import (
    AUDIO_DIR,
    VOICE_MEMOS_DIR,
    OutputRefused,
    checked_output_path,
    error_text,
    folder,
    json_text,
    media_line,
    secret,
    setting_number,
    setting_text,
    unique_name,
)
from indus.tools.builtin.media.providers import (
    ClientFactory,
    NoProvider,
    ProviderFailed,
    ProviderRegistry,
    active,
    default_client,
)
from indus.tools.registry import ToolRegistry

__all__ = [
    "MAX_BODY_BYTES",
    "SCHEMA",
    "TOOLSET",
    "OpenAISpeech",
    "PiperSpeech",
    "Speech",
    "SpeechTool",
    "TtsProvider",
    "clamp_speed",
    "split_text",
]

log = logging.getLogger(__name__)

TOOLSET = "tts"
CONFIG_KEY = "tts.provider"
MAX_BODY_BYTES = 16 * 1024 * 1024
MIN_SPEED, MAX_SPEED = 0.25, 4.0
SPEECH_TIMEOUT_S = 120.0
CLI_PLATFORMS = frozenset({"cli"})
CLI_RUN_CONTEXTS = frozenset({"interactive", "single_query"})
EXTENSIONS = {"mp3": "mp3", "opus": "ogg", "wav": "wav", "aac": "aac", "flac": "flac"}
FORMAT_OF_SUFFIX = {".mp3": "mp3", ".ogg": "opus", ".opus": "opus", ".wav": "wav",
                    ".aac": "aac", ".flac": "flac"}  # fmt: skip


@runtime_checkable
class TtsProvider(Protocol):
    name: str
    title: str
    max_chars: int
    speed_range: tuple[float, float]
    formats: tuple[str, ...]

    def missing(self, scope: Scope | None) -> str | None: ...

    async def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        fmt: str,
        *,
        scope: Scope | None,
        instructions: str | None = None,
    ) -> bytes: ...


# -- text and speed -----------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def _hard_pieces(text: str, cap: int) -> list[str]:
    words = text.split(" ")
    pieces: list[str] = []
    current = ""
    for word in words:
        while len(word) > cap:  # one "word" longer than the cap: cut it
            if current:
                pieces.append(current)
                current = ""
            pieces.append(word[:cap])
            word = word[cap:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= cap:
            current = candidate
        else:
            pieces.append(current)
            current = word
    if current:
        pieces.append(current)
    return pieces


def split_text(text: str, cap: int) -> list[str]:
    """``text`` with whitespace folded, cut into ordered pieces of at most ``cap`` characters.

    Pieces end at sentence ends where possible; joining them with single spaces gives the folded
    text back.
    """
    folded = " ".join(text.split())
    if not folded:
        return []
    if len(folded) <= cap:
        return [folded]
    units: list[str] = []
    for sentence in _SENTENCE_END.split(folded):
        units.extend([sentence] if len(sentence) <= cap else _hard_pieces(sentence, cap))
    pieces: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}" if current else unit
        if len(candidate) <= cap:
            current = candidate
        else:
            pieces.append(current)
            current = unit
    if current:
        pieces.append(current)
    return pieces


def clamp_speed(value: object, provider_range: tuple[float, float]) -> float:
    usable = isinstance(value, int | float) and not isinstance(value, bool)
    speed = float(value) if usable else 1.0  # type: ignore[arg-type]
    speed = min(max(speed, MIN_SPEED), MAX_SPEED)
    low, high = provider_range
    return min(max(speed, low), high)


# -- providers ----------------------------------------------------------------------------------


async def _capped_body(response: httpx.Response, label: str) -> bytes:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ProviderFailed(f"{label} sent more than {MAX_BODY_BYTES // (1024 * 1024)} MB")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise ProviderFailed(f"{label} sent more than {MAX_BODY_BYTES // (1024 * 1024)} MB")
    return bytes(body)


class OpenAISpeech:
    """Any OpenAI-compatible speech endpoint (see the module notes)."""

    name: str = "openai"
    title: str = "OpenAI-compatible speech API"
    max_chars: int = 4096
    speed_range: tuple[float, float] = (0.25, 4.0)
    formats: tuple[str, ...] = ("mp3", "opus", "wav", "aac", "flac")
    default_model = "gpt-4o-mini-tts"
    default_voice = "alloy"
    default_base_url = "https://api.openai.com/v1"
    default_key = "OPENAI_API_KEY"

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self.client_factory = client_factory or default_client

    def key_name(self, scope: Scope | None) -> str:
        return setting_text(scope, "tts.key_name", self.default_key) or self.default_key

    def missing(self, scope: Scope | None) -> str | None:
        name = self.key_name(scope)
        return None if secret(scope, name) else f"{name} is not set"

    async def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        fmt: str,
        *,
        scope: Scope | None,
        instructions: str | None = None,
    ) -> bytes:
        key = secret(scope, self.key_name(scope))
        if not key:
            raise ProviderFailed(f"{self.key_name(scope)} is not set")
        base = (setting_text(scope, "tts.base_url") or self.default_base_url).rstrip("/")
        payload: dict[str, Any] = {
            "model": setting_text(scope, "tts.model") or self.default_model,
            "input": text,
            "voice": voice or self.default_voice,
            "response_format": fmt,
            "speed": speed,
        }
        if instructions:
            payload["instructions"] = instructions
        label = "the speech endpoint"
        try:
            async with (
                self.client_factory(scope, SPEECH_TIMEOUT_S) as client,
                client.stream(
                    "POST",
                    f"{base}/audio/speech",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                ) as response,
            ):
                if response.status_code >= 400:
                    detail = (await response.aread())[:300].decode("utf-8", "replace")
                    raise ProviderFailed(f"{label} answered HTTP {response.status_code}: {detail}")
                return await _capped_body(response, label)
        except httpx.TimeoutException as exc:
            raise ProviderFailed(f"{label} did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ProviderFailed(f"could not reach {label}: {exc}") from exc


class PiperSpeech:
    """The local Piper engine run as its own program (see the module notes)."""

    name: str = "piper"
    title: str = "Piper (local)"
    max_chars: int = 3000
    speed_range: tuple[float, float] = (0.5, 2.0)
    formats: tuple[str, ...] = ("wav",)
    install_hint = (
        "install the Piper engine so `piper` is on PATH (or set tts.piper_binary) and download a "
        "voice model (.onnx) for tts.piper_model; or set tts.provider: openai and store "
        "OPENAI_API_KEY"
    )

    def binary(self, scope: Scope | None) -> str | None:
        configured = setting_text(scope, "tts.piper_binary", "piper") or "piper"
        if "/" in configured:
            return configured if Path(configured).is_file() else None
        return audio.find_program(configured, scope)

    def missing(self, scope: Scope | None) -> str | None:
        if self.binary(scope) is None:
            return f"Piper is not installed: {self.install_hint}"
        model = setting_text(scope, "tts.piper_model")
        if not model:
            return f"tts.piper_model is not set: {self.install_hint}"
        if not Path(model).expanduser().is_file():
            return f"the Piper voice model {model} does not exist"
        return None

    async def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        fmt: str,
        *,
        scope: Scope | None,
        instructions: str | None = None,
    ) -> bytes:
        reason = self.missing(scope)
        binary = self.binary(scope)
        if reason or binary is None:
            raise ProviderFailed(reason or "Piper is not installed")
        model = str(Path(setting_text(scope, "tts.piper_model")).expanduser())
        return await run_blocking(self._run, binary, model, text, speed, scope, label="piper")

    @staticmethod
    def _run(binary: str, model: str, text: str, speed: float, scope: Scope | None) -> bytes:
        with tempfile.TemporaryDirectory(prefix="indus-piper-") as work:
            out = Path(work) / "speech.wav"
            argv = [binary, "--model", model, "--output_file", str(out)]
            argv += ["--length_scale", f"{1 / speed:.3f}"]
            try:
                done = audio.run_tool(argv, scope, stdin=text.encode("utf-8"), timeout=300)
            except (OSError, ValueError) as exc:
                raise ProviderFailed(f"Piper could not run: {exc}") from exc
            except Exception as exc:  # subprocess.TimeoutExpired and friends
                raise ProviderFailed(f"Piper failed: {type(exc).__name__}") from exc
            if done.returncode != 0 or not out.is_file():
                detail = done.stderr.decode("utf-8", "replace").strip()[-300:]
                raise ProviderFailed(f"Piper failed ({done.returncode}): {detail or 'no audio'}")
            if out.stat().st_size > MAX_BODY_BYTES:
                raise ProviderFailed(f"Piper wrote more than {MAX_BODY_BYTES // (1024 * 1024)} MB")
            return out.read_bytes()


def default_registry() -> ProviderRegistry[TtsProvider]:
    return ProviderRegistry[TtsProvider]("speech", [PiperSpeech(), OpenAISpeech()])


# -- the tool -----------------------------------------------------------------------------------

SCHEMA: dict[str, Any] = {
    "name": "text_to_speech",
    "description": (
        "Turn text into spoken audio and save it as a file. Long text is spoken in order in "
        "several pieces and joined into one file. The result includes a MEDIA:<path> line; "
        "include that line in your reply to send the audio to the user. The voice and engine "
        "come from the user's settings."
    ),
    "parameters": {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "What to say."},
            "output_path": {
                "type": "string",
                "description": "Optional file to write (absolute or relative path, no '..'); "
                "by default the audio goes to the profile's audio folder.",
            },
            "speed": {
                "type": "number",
                "minimum": MIN_SPEED,
                "maximum": MAX_SPEED,
                "description": "Speaking speed, 1.0 normal; the engine may narrow the range.",
            },
            "instructions": {
                "type": "string",
                "description": "Optional delivery hints (tone, accent) for engines that take them.",
            },
        },
    },
}


@dataclass(frozen=True)
class Speech:
    path: Path
    fmt: str
    provider: str
    chunks: int
    voice_compatible: bool
    note: str = ""


class SpeechError(RuntimeError):
    """Speech could not be made; the message is meant for the model."""


def _join(pieces: list[bytes], fmt: str) -> bytes:
    if len(pieces) == 1:
        return pieces[0]
    if fmt == "wav":
        return audio.join_wav(pieces)
    if fmt in ("mp3", "aac"):
        return b"".join(pieces)
    raise SpeechError(f"pieces in {fmt} cannot be joined")


class SpeechTool:
    """The ``text_to_speech`` handler and its provider registry."""

    def __init__(
        self,
        providers: ProviderRegistry[TtsProvider] | None = None,
        *,
        cwd: Path | None = None,
        find_ffmpeg: Callable[[Scope | None], str | None] = audio.find_ffmpeg,
    ) -> None:
        self.providers = providers if providers is not None else default_registry()
        self.cwd = cwd
        self.find_ffmpeg = find_ffmpeg

    def _target(
        self,
        scope: Scope,
        output_path: str,
        ext: str,
        platform: str | None,
        run_context: str | None,
    ) -> Path:
        if output_path:
            chosen = checked_output_path(output_path, scope, base=self.cwd)
            return chosen if chosen.suffix else chosen.with_suffix(f".{ext}")
        on_cli = platform in CLI_PLATFORMS or (platform is None and run_context in CLI_RUN_CONTEXTS)
        where = folder(scope, VOICE_MEMOS_DIR if on_cli else AUDIO_DIR)
        return where / unique_name("tts", ext, scope)

    async def speak(
        self,
        text: str,
        scope: Scope,
        *,
        output_path: str = "",
        speed: object = None,
        instructions: str | None = None,
        voice: bool = False,
        platform: str | None = None,
        run_context: str | None = None,
    ) -> Speech:
        """Make the audio file (see the module notes); ``SpeechError`` / ``OutputRefused``."""
        try:
            provider = active(self.providers, scope, CONFIG_KEY)
        except NoProvider as exc:
            raise SpeechError(str(exc)) from exc
        reason = provider.missing(scope)
        if reason:
            raise SpeechError(f"speech engine {provider.name!r} is not ready: {reason}")
        pieces = split_text(text, provider.max_chars)
        if not pieces:
            raise SpeechError("text is empty")
        wanted = ""
        if output_path.strip():
            wanted = FORMAT_OF_SUFFIX.get(Path(output_path.strip()).suffix.lower(), "")
        if wanted and wanted not in provider.formats:
            wanted = ""
        if not wanted:
            native_opus = voice and "opus" in provider.formats and len(pieces) == 1
            wanted = "opus" if native_opus else ("mp3" if "mp3" in provider.formats else "")
            wanted = wanted or provider.formats[0]
        target = self._target(scope, output_path, EXTENSIONS[wanted], platform, run_context)
        rate = clamp_speed(
            speed if speed is not None else setting_number(scope, "tts.speed", 1.0),
            provider.speed_range,
        )
        voice_name = setting_text(scope, "tts.voice")
        made: list[bytes] = []
        for index, piece in enumerate(pieces, start=1):
            try:
                made.append(
                    await provider.synthesize(
                        piece, voice_name, rate, wanted, scope=scope, instructions=instructions
                    )
                )
            except ProviderFailed as exc:
                where = f" (piece {index} of {len(pieces)})" if len(pieces) > 1 else ""
                raise SpeechError(f"{provider.title}{where}: {exc}") from exc
            if not made[-1]:
                raise SpeechError(f"{provider.title} returned no audio for piece {index}")
        joined = _join(made, wanted)
        await run_blocking(atomic_write_bytes, target, joined, label="speech save")
        note = ""
        fmt = wanted
        if voice and fmt != "opus":
            ffmpeg = self.find_ffmpeg(scope)
            if ffmpeg is None:
                note = (
                    f"ffmpeg is not installed, so the audio stays {fmt.upper()} and cannot be "
                    "sent as a voice bubble"
                )
            else:
                ogg = target.with_suffix(".ogg")
                failed = await run_blocking(audio.to_opus, ffmpeg, target, ogg, scope, label="opus")
                if failed is None:
                    target.unlink(missing_ok=True)
                    target, fmt = ogg, "opus"
                else:
                    note = f"converting to Opus failed, so the audio stays {fmt.upper()}: {failed}"
        return Speech(target, fmt, provider.name, len(pieces), voice and fmt == "opus", note)

    async def handle(
        self,
        args: Mapping[str, Any],
        *,
        scope: Scope | None = None,
        platform: str | None = None,
        run_context: str | None = None,
        voice: bool = False,
    ) -> str:
        if scope is None:
            return error_text("text_to_speech needs an active profile")
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return error_text("text is required")
        instructions = args.get("instructions")
        try:
            made = await self.speak(
                text,
                scope,
                output_path=str(args.get("output_path") or ""),
                speed=args.get("speed"),
                instructions=instructions if isinstance(instructions, str) else None,
                voice=voice,
                platform=platform,
                run_context=run_context,
            )
        except (SpeechError, OutputRefused) as exc:
            return error_text(f"text_to_speech failed: {exc}")
        except OSError as exc:
            return error_text(f"text_to_speech could not write the audio: {exc}")
        body: dict[str, Any] = {
            "success": True,
            "media": media_line(made.path),
            "path": str(made.path),
            "format": made.fmt,
            "provider": made.provider,
            "chunks": made.chunks,
            "voice_compatible": made.voice_compatible,
        }
        if made.note:
            body["note"] = made.note
        return json_text(body)

    def register(self, registry: ToolRegistry) -> None:
        registry.register("text_to_speech", TOOLSET, SCHEMA, self.handle, max_result_chars=4000)


DEFAULT_TOOL = SpeechTool()
