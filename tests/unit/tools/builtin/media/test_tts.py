"""text_to_speech: chunking, speed, saving, Opus, body cap, output path rules (P10-17)."""

from __future__ import annotations

import asyncio
import io
import json
import stat
import sys
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from indus.foundation.scope import EnvView, Scope
from indus.tools.builtin.media import audio
from indus.tools.builtin.media.providers import ProviderRegistry
from indus.tools.builtin.media.tts import (
    MAX_BODY_BYTES,
    OpenAISpeech,
    PiperSpeech,
    SpeechTool,
    TtsProvider,
    clamp_speed,
    default_registry,
    split_text,
)
from tests.fakes.agent import DictConfig
from tests.fakes.media_providers import FakeSpeech, install_fake_ffmpeg
from tests.fakes.secret_store import MemorySecretStore

SENTENCE = "The quick brown fox jumps over the lazy dog near the river bank today."


def long_text(chars: int) -> str:
    words: list[str] = []
    while len(" ".join(words)) < chars:
        words.append(SENTENCE)
    return " ".join(words)[:chars].rstrip() + "."


def prepared(
    scope: Scope,
    tmp_path: Path,
    config: Mapping[str, Any] | None = None,
    *,
    path_dirs: list[Path] | None = None,
    engine: str | None = "fakespeech",
) -> Scope:
    """A profile whose PATH holds only ``path_dirs`` (an empty folder by default) and whose
    ``tts.provider`` is ``engine`` unless ``config`` names one (None: the schema default)."""
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    folders = path_dirs if path_dirs is not None else [empty]
    env = EnvView({"PATH": ":".join(str(p) for p in folders), "HOME": str(tmp_path / "user")})
    data: dict[str, Any] = {
        k: dict(v) if isinstance(v, Mapping) else v for k, v in (config or {}).items()
    }
    if engine is not None:
        data.setdefault("tts", {}).setdefault("provider", engine)
    return scope.with_config(DictConfig(data)).with_env(env)


def tool_with(*providers: TtsProvider) -> SpeechTool:
    return SpeechTool(ProviderRegistry[TtsProvider]("speech", list(providers)))


def say(tool: SpeechTool, scope: Scope, **kwargs: Any) -> dict[str, Any]:
    args = {
        k: kwargs.pop(k) for k in ("text", "output_path", "speed", "instructions") if k in kwargs
    }
    args.setdefault("text", "Hello there.")
    return json.loads(asyncio.run(tool.handle(args, scope=scope, **kwargs)))


# -- text and speed -----------------------------------------------------------------------------


def test_chunks_10000_chars(scope: Scope, tmp_path: Path) -> None:
    text = long_text(10_000)
    pieces = split_text(text, 4096)
    assert len(pieces) == 3
    assert all(len(p) <= 4096 for p in pieces)
    assert " ".join(pieces) == text
    assert all(p.endswith(".") for p in pieces)  # cut at sentence ends

    fake = FakeSpeech()
    result = say(tool_with(fake), prepared(scope, tmp_path), text=text)
    assert result["chunks"] == 3
    assert [c["text"] for c in fake.calls] == pieces
    assert Path(result["path"]).read_bytes() == b"piece:1|piece:2|piece:3|"


def test_long_sentence_and_long_word() -> None:
    sentence = " ".join(["word"] * 50)  # 249 characters, no sentence end
    pieces = split_text(sentence, 60)
    assert all(len(p) <= 60 for p in pieces) and " ".join(pieces) == sentence
    assert split_text("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
    assert split_text("  spaced \n\n out  ", 100) == ["spaced out"]
    assert split_text("   ", 100) == []


def test_speed_clamped(scope: Scope, tmp_path: Path) -> None:
    assert clamp_speed(5.0, (0.25, 4.0)) == 4.0
    assert clamp_speed(5.0, (0.5, 2.0)) == 2.0
    assert clamp_speed(0.1, (0.25, 4.0)) == 0.25
    assert clamp_speed(True, (0.25, 4.0)) == 1.0
    assert clamp_speed("fast", (0.25, 4.0)) == 1.0
    fake = FakeSpeech()
    tool = tool_with(fake)
    say(tool, prepared(scope, tmp_path), speed=5.0)
    say(tool, prepared(scope, tmp_path, {"tts": {"speed": 1.5}}))
    assert [c["speed"] for c in fake.calls] == [4.0, 1.5]


# -- saving -------------------------------------------------------------------------------------


def test_bytes_saved_and_media_path_returned(scope: Scope, tmp_path: Path) -> None:
    fake = FakeSpeech()
    tool = tool_with(fake)
    target = prepared(scope, tmp_path)
    result = say(tool, target)
    saved = Path(result["path"])
    assert result["media"] == f"MEDIA:{saved}"
    assert saved.parent == scope.home / "cache" / "audio" and saved.suffix == ".mp3"
    assert saved.read_bytes() == b"piece:1|"
    assert (result["format"], result["provider"], result["voice_compatible"]) == (
        "mp3",
        "fakespeech",
        False,
    )
    on_cli = say(tool, target, run_context="interactive")
    assert Path(on_cli["path"]).parent == scope.home / "voice-memos"
    gateway = say(tool, target, platform="telegram", run_context="interactive")
    assert Path(gateway["path"]).parent == scope.home / "cache" / "audio"


def test_opus_without_ffmpeg_stays_mp3_with_note(scope: Scope, tmp_path: Path) -> None:
    result = say(tool_with(FakeSpeech()), prepared(scope, tmp_path), voice=True)
    assert result["format"] == "mp3" and Path(result["path"]).suffix == ".mp3"
    assert result["voice_compatible"] is False
    assert "ffmpeg is not installed" in result["note"]


def test_opus_through_fake_ffmpeg(scope: Scope, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    install_fake_ffmpeg(bin_dir)
    result = say(
        tool_with(FakeSpeech()), prepared(scope, tmp_path, path_dirs=[bin_dir]), voice=True
    )
    saved = Path(result["path"])
    assert saved.suffix == ".ogg" and saved.read_bytes() == b"OggSpiece:1|"
    assert result["format"] == "opus" and result["voice_compatible"] is True
    assert not saved.with_suffix(".mp3").exists()
    assert "note" not in result


def test_native_opus_needs_no_ffmpeg(scope: Scope, tmp_path: Path) -> None:
    fake = FakeSpeech(formats=("mp3", "opus"))
    result = say(tool_with(fake), prepared(scope, tmp_path), voice=True)
    assert fake.calls[0]["fmt"] == "opus"
    assert Path(result["path"]).suffix == ".ogg" and result["voice_compatible"] is True


def test_wav_pieces_joined_under_one_header(scope: Scope, tmp_path: Path) -> None:
    def wav(index: int, fmt: str) -> bytes:
        out = io.BytesIO()
        with wave.open(out, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(bytes([index]) * 1600)
        return out.getvalue()

    fake = FakeSpeech(formats=("wav",), max_chars=200, body=wav)
    result = say(tool_with(fake), prepared(scope, tmp_path), text=long_text(500))
    facts = audio.wav_facts(Path(result["path"]))
    pieces = len(split_text(long_text(500), 200))
    assert pieces > 1 and result["chunks"] == len(fake.calls) == pieces
    assert facts.frames == pieces * 800


def test_piece_failure_leaves_no_file(scope: Scope, tmp_path: Path) -> None:
    fake = FakeSpeech(fail_on=2)
    result = say(tool_with(fake), prepared(scope, tmp_path), text=long_text(10_000))
    assert result["success"] is False and "piece 2 of 3" in result["error"]
    audio_dir = scope.home / "cache" / "audio"
    assert not audio_dir.exists() or list(audio_dir.iterdir()) == []


# -- output path rules (R06.22) -----------------------------------------------------------------


def test_output_path_written_and_format_from_suffix(scope: Scope, tmp_path: Path) -> None:
    fake = FakeSpeech()
    tool = tool_with(fake)
    wanted = tmp_path / "out" / "hello.wav"
    wanted.parent.mkdir()
    result = say(tool, prepared(scope, tmp_path), output_path=str(wanted))
    assert result["path"] == str(wanted.resolve()) and fake.calls[0]["fmt"] == "wav"
    bare = say(tool, prepared(scope, tmp_path), output_path=str(tmp_path / "out" / "noext"))
    assert bare["path"].endswith("noext.mp3")


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        ("../escape.mp3", "'..'"),
        ("speech\nMEDIA:/etc/passwd", "control characters"),
        ("notes MEDIA:/etc/shadow.mp3", "MEDIA: directive"),
        ("/etc/cron.d/speech.mp3", "protected"),
        ("", None),
    ],
)
def test_output_path_refusals(scope: Scope, tmp_path: Path, raw: str, words: str | None) -> None:
    fake = FakeSpeech()
    result = say(tool_with(fake), prepared(scope, tmp_path), output_path=raw)
    if words is None:  # empty means "use the default folder"
        assert result["success"] is True
        return
    assert result["success"] is False and words in result["error"]
    assert fake.calls == []


def test_output_path_symlink_into_ssh_refused(scope: Scope, tmp_path: Path) -> None:
    user = tmp_path / "user"
    (user / ".ssh").mkdir(parents=True)
    link = tmp_path / "innocent"
    link.symlink_to(user / ".ssh")
    result = say(
        tool_with(FakeSpeech()), prepared(scope, tmp_path), output_path=str(link / "a.mp3")
    )
    assert result["success"] is False and ".ssh" in result["error"]
    assert list((user / ".ssh").iterdir()) == []


# -- built-in providers -------------------------------------------------------------------------


def _keyed(scope: Scope, tmp_path: Path, config: Mapping[str, Any] | None = None) -> Scope:
    fake_key = "test-" + "speech-key"
    base = {"tts": {"provider": "openai"}}
    base["tts"].update((config or {}).get("tts", {}))
    return prepared(scope, tmp_path, base).with_secrets(
        MemorySecretStore({"OPENAI_API_KEY": fake_key})
    )


@respx.mock
def test_openai_speech_request(scope: Scope, tmp_path: Path) -> None:
    route = respx.post("https://api.openai.com/v1/audio/speech").mock(
        return_value=httpx.Response(200, content=b"ID3-fake-mp3")
    )
    tool = SpeechTool(default_registry())
    target = _keyed(scope, tmp_path, {"tts": {"voice": "verse"}})
    result = say(tool, target, speed=5.0, instructions="calm")
    assert Path(result["path"]).read_bytes() == b"ID3-fake-mp3"
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer test-speech-key"
    assert json.loads(sent.content) == {
        "model": "gpt-4o-mini-tts",
        "input": "Hello there.",
        "voice": "verse",
        "response_format": "mp3",
        "speed": 4.0,
        "instructions": "calm",
    }


@respx.mock
def test_openai_body_cap(scope: Scope, tmp_path: Path) -> None:
    big = b"\0" * (17 * 1024 * 1024)
    respx.post("https://api.openai.com/v1/audio/speech").mock(
        return_value=httpx.Response(200, content=big)
    )
    result = say(SpeechTool(default_registry()), _keyed(scope, tmp_path))
    assert result["success"] is False and "more than 16 MB" in result["error"]
    assert MAX_BODY_BYTES == 16 * 1024 * 1024
    audio_dir = scope.home / "cache" / "audio"
    assert not audio_dir.exists() or list(audio_dir.iterdir()) == []


@respx.mock
def test_openai_http_error(scope: Scope, tmp_path: Path) -> None:
    respx.post("https://api.openai.com/v1/audio/speech").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    result = say(SpeechTool(default_registry()), _keyed(scope, tmp_path))
    assert "HTTP 401" in result["error"]
    assert OpenAISpeech().missing(prepared(scope, tmp_path)) == "OPENAI_API_KEY is not set"


def test_piper_is_the_default_and_says_what_is_missing(scope: Scope, tmp_path: Path) -> None:
    result = say(SpeechTool(default_registry()), prepared(scope, tmp_path, engine=None))
    assert result["success"] is False
    assert "'piper' is not ready" in result["error"] and "Piper is not installed" in result["error"]


FAKE_PIPER = """#!{python}
import sys, wave
args = sys.argv[1:]
out = args[args.index("--output_file") + 1]
scale = args[args.index("--length_scale") + 1]
text = sys.stdin.read()
with open(out + ".args", "a") as log:
    log.write(scale + "|" + text + "\\n")
with wave.open(out, "wb") as writer:
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(16000)
    writer.writeframes(b"\\x01\\x00" * len(text))
"""


def test_piper_runs_as_a_separate_program(scope: Scope, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    piper = bin_dir / "piper"
    piper.write_text(FAKE_PIPER.format(python=sys.executable))
    piper.chmod(piper.stat().st_mode | stat.S_IXUSR)
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"onnx")
    target = prepared(
        scope, tmp_path, {"tts": {"piper_model": str(model)}}, path_dirs=[bin_dir], engine=None
    )
    assert PiperSpeech().missing(target) is None
    result = say(SpeechTool(default_registry()), target, text="Hi. " * 900, speed=2.0)
    saved = Path(result["path"])
    assert result["provider"] == "piper" and saved.suffix == ".wav"
    assert result["chunks"] == 2
    assert audio.wav_facts(saved).frames == sum(len(p) for p in split_text("Hi. " * 900, 3000))
