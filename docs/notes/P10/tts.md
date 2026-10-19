# Task: `text_to_speech` (P10-17) Phase: 10 Owner: P10 slice J builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/tts_tool.py` (flow, output
path rules, voice-bubble finalisation, chunk loop), `tools/tts_tool_providers.py` (body cap, OpenAI
shape, speed ranges), `tools/tts_tool_delivery.py` (skim: sentence split). Closed before writing.
Evidence: none.

What it does (own words): turns text into an audio file and answers with `MEDIA:<path>` so a
surface can deliver it. A provider implements `synthesize(text, voice, speed, fmt) -> bytes` and
declares its per-request character cap, speed range and the formats it can emit. Long text is
split at sentence ends into ordered pieces under the provider's cap (a sentence longer than the cap
is split at word boundaries), each piece is synthesised in order and the audio is joined (MP3
frames append; WAV pieces are merged under one header). The speed is clamped to the tool's
0.25–4.0 and then to the provider's own range. Every response body is read with a 16 MB cap.

Built-ins: `openai` — any OpenAI-compatible `/audio/speech` endpoint (4,096 characters per request,
speed 0.25–4.0, emits mp3/opus/wav/aac/flac, key from the secret store); `piper` — the local Piper
engine run as a separate process (the `piper` executable, MIT licence; text on stdin, WAV out,
voice model from `tts.piper_model`). **Default provider: `piper` (D-16.71).** It needs no key and no
network; hosted engines are opt-in with the user's key. Piper being absent gives one clear message
saying what to install or how to pick `openai`.

Output: `<home>/cache/audio/tts-<stamp>.<ext>`; on the CLI `<home>/voice-memos/`. A caller-given
`output_path` must be a plain path (no control characters, no `..`, no embedded `MEDIA:`
directive); it is resolved through symlinks and refused when it lands on a protected credential or
system path or an Indus secret/control file (R06.22). When the session wants a voice bubble
(`voice` flag), Opus is asked from a provider that emits it; otherwise the joined audio is
converted with `ffmpeg`; with no `ffmpeg` the file stays MP3 (or WAV) and the result carries a note.

Inputs → outputs: `{text, output_path?, speed?, instructions?}` + host keywords (`scope`,
`platform`, `run_context`, `voice`) → JSON `{success, media: "MEDIA:<path>", path, format, provider,
chunks, voice_compatible, note?}` or `{error}`.

Files: `src/indus/tools/builtin/media/tts.py`, `media/audio.py` (WAV join, ffmpeg run, shared with
STT), `tests/unit/tools/builtin/media/test_tts.py`, `tests/fakes/media_providers.py`.
Shared: `src/indus/tools/toolsets.py` (`tts`, core gets `text_to_speech`),
`src/indus/foundation/config/schema.py` (`tts`) and its test.

Edge cases / failure modes:
- empty text or text empty after whitespace folding;
- 17 MB body → refused, nothing written;
- a chunk fails → the whole call fails, no partial file left;
- piper missing, model missing, piper exits non-zero → plain errors;
- `output_path` symlink to `~/.ssh/...` → refused after resolving.

Our design differences / decisions:
- Default local engine Piper (MIT, separate process) instead of the reference's keyless hosted
  default (D-16.71). Piper bundles eSpeak NG (GPL-3.0) inside its own executable; we only run it as
  a separate process, the same ruling as signal-cli.
- One output file per call (no per-platform upload packing).
- No markdown/emoji speech cleanup beyond whitespace folding (future).
- Built-ins only `openai` and `piper`; plugin providers register into `TTS_PROVIDERS` (P13).

Tests we will write:
- 10,000-char text → 3 ordered chunks under 4,096; long single sentence split at words;
- speed 5.0 clamped to the provider maximum; 0.1 to the minimum;
- fake provider bytes saved and `MEDIA:` path returned (cache and CLI voice-memos);
- Opus requested, fake ffmpeg absent → mp3 + note; fake ffmpeg present → `.ogg`;
- 17 MB response from the respx OpenAI endpoint rejected;
- output path policy: traversal, directive, symlink into `.ssh`.

## Decisions for the lead
- Host keywords: `platform` (the surface name) and `voice` (voice-bubble wanted) are read from
  handler extras when hosts offer them; the CLI default (`run_context` interactive/single_query)
  saves into `voice-memos/`.
- The full outbound media path policy (safe roots, strict mode, recent-file trust) is P15-18's;
  this tool only guarantees what it writes and returns is a resolved, non-protected path.

Compare checklist (filled after the rewrite):
- [x] per-provider cap and ordered chunks (test: `test_tts.py::test_chunks_10000_chars`)
- [x] 16 MB body cap (test: `test_openai_body_cap`)
- [x] Opus via native format or ffmpeg, mp3 + note without ffmpeg (tests: `test_opus_*`)
- [x] output path guards incl. media-directive injection (tests: `test_output_path_*`)
- Deliberate differences: Piper default; one file per call; no audio-tag rewriting; no
  `[[audio_as_voice]]` marker in the text — the result's `voice_compatible` field tells the
  sender (P15) whether the file can go out as a voice bubble.
- Not taken: ElevenLabs/xAI/MiniMax/Mistral/Gemini/Edge/command providers (plugins later).

## Status

Done (2026-10-19).
