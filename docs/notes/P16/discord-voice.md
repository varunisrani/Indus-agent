# Task: Discord voice channels (P16-35) Phase: 16 Owner: wave-28 builder E17

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/discord/voice_mixer.py`
(all: frame geometry, mixer children, ducking, decode helper, ambient synthesis),
`plugins/platforms/discord/adapter.py` (voice parts only: the `/voice` command row, the voice receiver
class — packet hook, decryption order, per-speaker buffers, silence check, flush, PCM to WAV — the
`on_voice_state_update` handler, the voice timeout readers, join / leave / play, the listen loop,
keepalive, `_process_voice_input`), `gateway/run_voice.py` (voice channel parts: join and leave
handlers, the timeout cleanup, duplicate transcript suppression, the voice input source and its
authorization, the voice reply path). Evidence: none. Vendor: discord.py 2.7.1 source (MIT) from
the offline uv cache — `VoiceClient.create_connection_state`, `VoiceConnectionState`
(`add_socket_listener`, `send_packet`, `secret_key`, `mode`, `dave_session`,
`dave_protocol_version`, `hook`), `DiscordVoiceWebSocket` (op 5 SPEAKING, op 13 CLIENT_DISCONNECT,
`_hook(ws, msg)`), the `aead_xchacha20_poly1305_rtpsize` packet layout of
`_encrypt_aead_xchacha20_poly1305_rtpsize`, `opus.Decoder.decode`, `AudioSource`,
`abc.Connectable.connect(cls=)`; its `voice` extra (`PyNaCl>=1.5.0,<1.6`, `davey>=0.1.0`).

Pinned library: extra `discord-voice` = `discord.py[voice]==2.7.1` (the same discord.py pin as the
`discord` extra; Discord voice gateway v8). Transitives the lock will add (not in the offline uv
cache, so not resolved here; the lead re-locks): **PyNaCl** (Apache-2.0, bundles libsodium, ISC;
`<1.6` leaves 1.5.0), **davey** (Discord's DAVE end-to-end voice encryption, Rust wheel, MIT per its
repository — to confirm from the dist-info at re-lock). cffi (MIT-0, recorded exception) is already
locked. System needs: `ffmpeg` (speech playback) and `libopus` (discord.py's Opus codec).

What it does (own words): in a Discord server, an allowed person types `/voice join` (as text or as
the application command) while sitting in a voice channel. The bot joins that voice channel and
listens. Each person's audio is collected separately; when someone has been quiet for 0.8 s, what
they said (at least 0.5 s of it, at most 60 s per piece) becomes one utterance, which is turned into
16 kHz mono WAV, transcribed with the profile's speech-to-text provider, and handed to the gateway as
a normal inbound message keyed on the voice channel (chat id = the voice channel, chat type group,
the speaker as the user), marked as addressed to us. Only allowed people are heard at all: audio from
anyone else is dropped before transcription, and the gateway's authorization still checks every
turn. Replies the gateway sends to that voice channel are posted in its text chat as usual and are
also spoken: the text goes through the profile's text-to-speech, ffmpeg decodes the result to
48 kHz stereo PCM, and a mixer plays it (several replies overlap instead of cutting each other off).
`/voice leave` leaves; `/voice status` says where the bot listens, for how long, and how long it
will stay without speech. With nobody speaking (and nothing played) for 300 s the bot leaves on its
own and says so in the channel. Text chat keeps working throughout, and when ffmpeg, libopus or the
voice extra are missing, `/voice join` explains what to install and the adapter carries on.

Inputs → outputs: `/voice join|leave|status` in a guild (text or application command), the caller's
voice state, `platforms.discord.voice_idle_seconds` (default 300, 0 = never) → a joined voice
connection, status replies; per-speaker PCM frames → `InboundEvent`s (text = transcript); outbound
`send` to a joined voice channel → text post plus speech.

Files: `src/indus/gateway/adapters/discord/voice.py` (new), `tests/unit/gateway/adapters/discord/test_voice.py`
(new). Shared: `src/indus/gateway/adapters/discord/adapter.py` (the `/voice` intercept, speech on
send, leave on close, the voice vendor names, the command hint), `__init__.py` (voice deps from the
profile scope), `pyproject.toml` (extra `discord-voice`), `THIRD_PARTY_NOTICES.md` and the 00-layout
§4 pin table (PyNaCl, davey rows), `docs/internals/acceptance-map.md` (ACC-K45-1).

Edge cases / failure modes:
- `/voice join` in a DM, by someone not allowed, or by someone not in a voice channel: a short
  answer, nothing joined.
- ffmpeg missing, libopus missing, PyNaCl / davey missing: a clear answer naming the fix; no join.
- already in a voice channel of this server: the same channel answers "already listening"; another
  channel leaves the old one first (its pending speech is dropped, never sent to the new one).
- joining fails (permissions, timeouts, Discord errors): the answer names the error (scrubbed).
- a speaker who never goes quiet: cut at 60 s so a piece stays inside STT size limits.
- under 0.5 s of sound (a cough, a click): dropped.
- empty, inaudible or failed transcripts: dropped (logged); the same text from the same speaker
  again within 12 s: dropped (capture glitches).
- an unknown SSRC (no SPEAKING mapping yet): the frame is dropped (with DAVE active it is still
  ciphertext; without a speaker it cannot be attributed). Discord sends SPEAKING before audio, so
  this costs at most the first frames after a rejoin.
- RTCP and non-Opus packets, malformed RTP, failed decryption, Opus errors: the packet is dropped
  (the decoder for that SSRC is reset on an Opus error).
- disconnect / gateway stop: every voice connection is left first.
- idle: allowed speech, our own playback, and a join all count as activity; the timer runs on the
  adapter's clock.

Our design differences / decisions:
- turns are keyed on the voice channel itself (Discord voice channels have their own text chat),
  not on the text channel `/voice join` was typed in; replies there are both posted and spoken.
- `/voice join|leave|status` are handled by the Discord adapter (no core change); `/voice on|off`
  still go to the shared command. The adapter's own gate for joining: the platform or global
  allow-all switch, a user allowlist (platform, global, `allow_from`), an allowed role, or a pairing
  approval — stricter than button presses, which defer to the core chain when no role list is set.
- spoken turns carry `allow_control=False`: a transcript never runs a slash command.
- one mixer per connection that plays only while it has speech (discord.py's player stops on an
  empty read and restarts on the next reply), no ambient bed, no spoken acknowledgements.
- receive path written against discord.py's public-ish seams: a `VoiceClient` subclass whose
  `create_connection_state` passes our voice-websocket hook (SPEAKING → SSRC map) and a socket
  listener; transport decryption is PyNaCl's AEAD (`nacl.secret.Aead`), end-to-end decryption is
  the connection's `davey` session; no cryptography of our own.
- 16 kHz mono WAV made in Python (downmix + 3:1 average), so transcription needs no ffmpeg.
- transcripts are echoed into the voice channel's text chat with mentions disabled.

Tests we will write (`test_voice.py`, fake voice link, fake clock, no discord.py):
- two speakers → two utterances → two turns, each keyed on the voice channel and marked mentioned;
  a stranger's audio never reaches STT.
- idle leave at 300 s on the fake clock (299 s still joined; speech resets it; notice posted).
- missing ffmpeg / missing voice libraries → clear answer, nothing joined, text send still works.
- join / leave / status answers (DM, not in a voice channel, not allowed, already joined, moved).
- silence gap 0.8 s, minimum 0.5 s, 60 s cut; duplicate transcript suppression.
- RTP parse and payload opening with a fake opener (extension header, padding, RTCP, bad version).
- mixer: one clip in 20 ms frames, two clips summed and clamped, empty read when idle.
- WAV conversion (16 kHz mono, length), speech on send to the joined channel only.
- adapter wiring: `/voice join` as text and as an application command, leave on disconnect.

## Decisions for the lead
- Extra name `discord-voice` = `discord.py[voice]==2.7.1` (exact pin on discord.py; PyNaCl and davey
  come in through the lock). Re-lock needed: `uv lock` online (PyNaCl / davey metadata are not in
  the offline cache); confirm davey's licence and the newest stable davey ≥ 14 days old.
- Turns keyed on the voice channel (row) instead of the reference's bound text channel.
- `platforms.discord.voice_idle_seconds` read from the free-form platform block (no schema row).
- `/voice status` in a server answers the voice-channel status (the shared `/voice status` keeps
  answering in DMs and other platforms).

Compare checklist (filled after the rewrite; reopened the files listed under Read):
- [x] RTP: version 2 and payload type 0x78 only, CSRC and extension header in the clear
  authenticated part, 4-byte nonce tail zero-padded to 24, extension body stripped after opening,
  RTP padding stripped (bad pad length dropped), our own SSRC ignored (test:
  `test_voice.py::test_rtp_packets_are_parsed_and_opened`, `::test_the_receiver_decodes_known_speakers_only`)
- [x] DAVE after transport decryption, keyed on the speaker's user id; unmapped SSRC dropped while
  DAVE is on; Opus error resets that SSRC's decoder (`::test_the_receiver_decodes_known_speakers_only`)
- [x] SPEAKING maps SSRC → user, CLIENT_DISCONNECT forgets the user (the hook in `DiscordVoiceLink`)
- [x] per-speaker buffers, minimum 0.5 s, silence gap (row: 0.8 s) (`::test_silence_gap_minimum_and_maximum`)
- [x] only allowed speakers transcribed; role grant computed for the speaker, not the joiner
  (`::test_two_speakers_become_two_turns_keyed_on_the_voice_channel`, `::test_a_strangers_speech_does_not_keep_the_bot`)
- [x] speech captured for one binding never lands in another (left / moved during transcription
  drops it) (`_utterance` session check; `::test_join_leave_and_status_answers` moves channels)
- [x] duplicate transcripts within 12 s dropped (`::test_a_repeated_transcript_is_dropped`)
- [x] transcript echoed into the channel with mentions disabled (`::test_typed_voice_join_joins_hears_and_speaks_replies`)
- [x] 300 s inactivity leave with a notice; speech and playback count as activity
  (`::test_the_bot_leaves_after_300_seconds_without_speech`, `::test_speech_restarts_the_idle_clock`)
- [x] UDP keepalive every 15 s (`::test_the_bot_leaves_after_300_seconds_without_speech` counts 19)
- [x] missing PyNaCl / davey / libopus / ffmpeg → install hint, no join, text unaffected
  (`::test_missing_ffmpeg_is_a_clear_answer_and_nothing_is_joined`, `::test_missing_voice_libraries_name_the_extra`,
  `::test_the_library_check_names_the_extra_or_libopus`, `::test_missing_ffmpeg_answers_and_text_keeps_working`)
- [x] leave voice before the client closes (`_close`; `::test_typed_voice_join_joins_hears_and_speaks_replies`)
- [x] Markdown stripped before speech (`::test_replies_to_the_joined_channel_are_spoken_in_order`)
- Deliberate differences: turns keyed on the voice channel (the reference binds them to the text
  channel `/voice join` was typed in); silence gap 0.8 s (row) instead of 1.5 s; a join in another
  channel leaves and rejoins instead of moving; speech still buffered at `/voice leave` is dropped
  (the reference transcribes it); frames inside a DAVE passthrough window are dropped rather than
  passed through (bounded loss, no reliance on the library's error wording); transcripts are
  compared exactly after normalising (no fuzzy ratio); replies are queued one after another in the
  mixer (no ducking); every reply to the joined channel is spoken (no per-chat voice mode).
- Not taken: the ambient bed, spoken acknowledgements and lead silence (`voice_fx`, off by default
  in the reference); sole-member SSRC inference after a rejoin; pausing capture while speaking (the
  bot never hears itself); `/voice off` keeping the bot in the channel (no gateway voice modes yet);
  the reference's speech-hallucination word list (our STT reports inaudible clips itself); periodic
  decrypt-failure logging (counted in `VoiceReceiver.dropped`).

## Status

Done (2026-10-20), receive path over real discord.py not exercised offline (the extra is not in the
offline cache: PyNaCl / davey). Shared files changed: `discord/adapter.py` (voice intercept, speech on
send, leave on close, vendor names, `voice_states` intent, command hint), `discord/__init__.py`
(voice deps), `tests/fakes/platform/discord_wire.py` (members' voice states), `pyproject.toml`
(extra), `THIRD_PARTY_NOTICES.md`, 00-layout §4, acceptance map (ACC-K45-1). Follow-ups: speak the
final edit instead of the first send once the runner streams replies; a live lane in a real server.

Integration (2026-10-20): the `discord-voice` extra was re-locked online (PyNaCl 1.5.0, davey 0.1.6,
MIT by its wheel's classifier); D-16.560, D-16.561.
