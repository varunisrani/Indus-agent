# Task: Speech-to-text library (P10-18) Phase: 10 Owner: P10 slice J builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/transcription_tools.py`
(routing, caps, credential guard), `tools/transcription_chunking.py` (silence split, lookback),
`tools/transcription_local.py` (skim: faster-whisper call, hallucination filter). Closed before
writing.
Evidence: none.

What it does (own words): a library call, not a model tool: `transcribe(path, scope=…)` gives a
`Transcript{text, language, duration, inaudible, provider, pieces}`. Providers implement
`transcribe(path, language?)` and record their own limits (bytes per request, seconds per
request). Built-ins: `local` (faster-whisper, the default when the `voice` extra is installed;
model size `stt.local_model`), `openai` (any OpenAI-compatible `/audio/transcriptions`, 25 MB) and
`groq` (same API shape, 25 MB). Without a configured provider the first ready one is used in that
order. A file over the provider's byte cap, or longer than its seconds cap, is cut at silences:
for each piece the cut goes to the latest pause found within the lookback window (at most 60 s)
before the ideal cut, else at the ideal cut; pieces are transcribed in order and joined. Only PCM
WAV can be cut here; any other format over the cap is refused with a plain message. An empty or
inaudible result is flagged (`inaudible=True`) and its text is a fixed "nothing could be heard"
note, never an empty string (R06.23). Provider failures become plain messages (`SttError`).

Inputs → outputs: path, optional language/provider → `Transcript` or `SttError(message)`.

Files: `src/indus/tools/builtin/media/stt.py`, `media/audio.py` (WAV facts, silence finding,
splitting), `tests/unit/tools/builtin/media/test_stt.py`, `tests/fakes/media_providers.py`.
Shared: `pyproject.toml` (extra `voice = ["faster-whisper==1.2.1"]`),
`src/indus/foundation/config/schema.py` (`stt`) and its test.

Edge cases / failure modes:
- missing, empty or secret-bearing file (read guard as `read_file`) → refused before any upload;
- 30 MB MP3 for a 25 MB provider → refused (cannot cut non-WAV without ffmpeg);
- a piece fails → the whole call fails with "piece i of n";
- faster-whisper not installed and `local` chosen → message naming the `voice` extra;
- 401/403 → key refused; 413 → too large; 429 → rate limited; timeout / connection → plain text.

Our design differences / decisions:
- Silence detection is our own pure-Python RMS scan over 16-bit/8-bit PCM WAV (no ffmpeg); other
  formats are not re-encoded here.
- No Mistral/xAI/ElevenLabs/DeepInfra/command providers (plugins later); no idle model unload
  watcher; the local model is cached per (size, device) for the process.
- `inaudible` text is a fixed note; voice mode and the gateway check the flag.

Tests we will write:
- 30 MB file refused for a 25 MB fake provider;
- synthetic 3-minute WAV with pauses, 60 s per request → cuts within 1 s of the pauses, pieces in
  order, texts joined;
- empty transcript → `inaudible=True`, non-empty text;
- provider HTTP errors (respx) → plain messages; secret file refused; auto choice order.

## Decisions for the lead
- `faster-whisper==1.2.1` (MIT) pulls `av` (BSD-3-Clause, wheels bundle LGPL FFmpeg libraries),
  `ctranslate2` (MIT), `onnxruntime` (MIT), `tokenizers`/`huggingface-hub`/`hf-xet` (Apache-2.0),
  `numpy` (BSD-3-Clause), `tqdm` (MPL-2.0 AND MIT). The licence check walks only the main
  dependencies; please confirm the extra by hand and re-lock (uv.lock re-generated only in this
  worktree to test).

Compare checklist (filled after the rewrite):
- [x] 25 MB caps per provider (test: `test_stt.py::test_cap_refuses_30mb_non_wav`)
- [x] silence cuts with ≤ 60 s lookback, joined in order (test: `test_three_minute_wav_split_near_silences`)
- [x] empty transcript flagged (test: `test_empty_is_inaudible`)
- [x] secret files never uploaded (test: `test_secret_missing_and_empty_files_refused`)
- Deliberate differences: no re-encode to m4a before splitting; WAV only.
- Not taken: hallucination phrase list, CAF/SILK conversion, pre-upload silence trim.

## Status

Done (2026-10-19).
