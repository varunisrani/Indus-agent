# Task: Intake — voice, images, documents, albums (P15-23) Phase: 15 Owner: wave-2 builder W2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run_voice.py` (transcription
flow), `gateway/run_inbound.py` (media enrichment only: classification, image routing decision,
vision pre-analysis, transcription with local fallback and per-clip memo, document and media-file
notes), `gateway/platforms/base.py` (cache helpers 494–780: size cap, per-kind cache dirs 0700,
owner-only files, image magic check, capped streaming download). Research 06 §3.12; trap R06.23.
P10 `tools/builtin/media/stt.py` (`transcribe`, `Transcript`, `INAUDIBLE_TEXT`) and `vision.py`;
P15-18 media policy (`CACHE_DIRS`). Closed before writing.
Evidence: none.

What it does (own words): turns the attachments of an inbound chat message into text the agent
can use, keeping every downloaded byte inside the profile's gateway caches.

Caches: `<home>/cache/images|audio|video|documents`, folders 0700, files 0600 created exclusively
(no following of an existing name or symlink), named `<kind>_<random>.<ext>` — the sender's file
name is only used for its (allow-listed) extension and a cleaned display name, never as a path.
Inbound cap 128 MiB (`134217728` bytes): a declared size over it is refused before reading, a
stream is counted as it is written and the partial file removed when it goes over. Types: images
must start with an image signature (an HTML error page is refused), audio/video/documents must
have an allow-listed extension or MIME type; anything else gets a "not supported" note. A local
attachment path is used only when it resolves to a regular file inside one of those caches (an
adapter that already downloaded); any other local path is refused, never opened. Remote refs are
fetched through the platform's registered fetcher into the cache; nothing is ever executed.

Voice/audio: transcribe with the configured STT provider (P10), then the local provider; an empty,
inaudible or failed transcript inserts one note that nothing could be made out and the agent should
ask for it again (R06.23); success inserts the transcript as a plain quoted line, nothing else;
`gateway.stt_enabled: false` → a note with the path and the duration when known. Clips are memoised
(path + size + mtime) so a merged burst or a re-run never transcribes a clip twice.
Images: per turn, from the effective model (one-turn → session → channel override → default
route): `native` when that model reads images, else `text` (a side-model description prepended,
P10 `vision_analyze` text route); any error in the decision or the description falls back to a
text note pointing at the cached file.
Documents: text-like files ≤100 KiB are inlined in a fenced block; other supported types get a
context note with the cached path, type and size; image documents are treated as images.
Albums and photo bursts: media messages from one sender in one chat (same album id, or photos with
no album id) are held for a 0.8 s quiet window (at most 3 s) and merged into one event, captions
merged (P15-12 `merge_events`); text from the same sender flushes the held burst first.
Send-time stamps (`gateway.message_timestamps.enabled`, default false) go only into the model text.

Inputs → outputs: `MediaCache(scope, max_bytes=...)` (`folder`, `save`, `admit`);
`VoiceIntake(scope, transcriber=None, enabled=None)` (`notes_for(paths, event) -> list[str]`);
`MediaIntake(scope, *, pipeline=None, cache=None, voice=None, describe=None, sees_images=None,
fetchers=None)` (`enrich(event) -> str`, `register_fetcher(platform, fn)`);
`AlbumCollector(clock, forward)` (`front_door(event) -> bool`, `aclose()`).

Files: `src/indus/gateway/intake/__init__.py`, `src/indus/gateway/intake/media.py`,
`src/indus/gateway/intake/voice.py`, `tests/unit/gateway/intake/test_media.py`,
`tests/unit/gateway/intake/test_voice.py`; shared: `src/indus/foundation/config/schema.py` (+test)
for `gateway.stt_enabled`.

Edge cases / failure modes:
- 128 MiB + 1 byte declared → refused before any read; a lying stream → partial file removed.
- symlink planted in a cache folder pointing at `~/.ssh/id_ed25519` → refused (lstat + resolved
  path inside the cache).
- relative path, `..`, `/etc/passwd` as an attachment ref → refused, never opened.
- non-UTF-8 "text" file → treated as binary (note, not inlined).
- STT raises in both providers → the inaudible note (never an exception into the turn).
- more than 10 attachments on one message → the rest get a "skipped" note.

Our design differences / decisions:
- "native" image mode: our `SessionService.submit` takes text only, so a native turn gets a note
  with the cached path and the instruction to look with `vision_analyze`, whose native route shows
  the pixels to the main model. Real image parts on the user message need a P04 submit seam
  (follow-up for the lead).
- Audio files sent as documents get a file note, not a transcript (only voice/audio messages are
  transcribed).
- Album quiet window 0.8 s / cap 3 s (ours).
- Types are extension sets per cache; MIME → extension uses the stdlib's built-in table
  (`mimetypes.MimeTypes()`, no system files) plus a few chat types it lacks. Document notes name
  the type by extension ("PDF file").

Tests we will write: fake STT returning "", raising (both providers), and text → inaudible note,
fallback used, quoted line; STT disabled note with duration; 128 MiB + 1 byte refused (declared)
and a stream over a small cap removed; memoisation on a merged burst (STT called once per clip);
image mode switch per model capability and text fallback on describer error; text document
inlined, large or binary document noted, image document routed as image; symlink and foreign path
refused; HTML bytes as an image refused; album merge with captions; timestamps only when enabled.

## Decisions for the lead
- New config row `gateway.stt_enabled` (default true) in the one schema.
- Default image describer = P10 `VisionTool` text route with the service's aux client.
- Fetchers are registered per platform by the adapters (P15-33..35); with none, a remote ref gets a
  "could not be downloaded" note.

Compare checklist (filled after the rewrite; reopened `gateway/run_inbound.py` media enrichment,
`gateway/run_voice.py`, cache helpers in `gateway/platforms/base.py`):
- [x] cap checked on a declared size and on the running total (`tests/unit/gateway/intake/test_media.py::test_the_128_mib_cap`)
- [x] cache dirs 0700, files 0600, random names; non-image bytes refused as images (`::test_downloads_land_owner_only_under_random_names`, `::test_types_are_checked`)
- [x] configured STT then local fallback; empty → explicit note; success → plain quoted line; STT off
  → path + duration; duplicate clips once; per-clip memo (`tests/unit/gateway/intake/test_voice.py::*`)
- [x] image routing native/text per turn, description + path for a closer look, failure → pointer
  (`::test_image_mode_follows_the_model`, `::test_the_vision_probe_reads_the_effective_route`)
- [x] document notes for every non-media file; small text inlined (`::test_documents`)
- [x] audio files and videos as path notes, not transcribed (`::test_files_never_leave_the_profile`)
- [x] albums / bursts merged with captions (`::test_an_album_becomes_one_event`)
- [x] local paths outside the caches never read (ours: the reference re-homes adapter paths)
  (`::test_foreign_paths_are_refused_and_never_read`, `::test_only_regular_files_inside_the_caches_are_admitted`)
- Not in P15: echoing transcripts back to the chat (reference option), cache age cleanup (the
  reference prunes after a max age; follow-up for the daemon housekeeping, P15-24/25), Docker
  container-path translation of cache paths (P11 follow-up already open).
