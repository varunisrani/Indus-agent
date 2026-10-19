# Task: CLI voice mode (P10-19) Phase: 10 Owner: wave 7 builder V

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/voice_mode.py` (CLI flow
only: recorder, silence detection, transcription hand-off, playback, requirement check; the
streaming, barge-in, wake-word and phone-OS parts skimmed and left out),
`hermes_cli/cli_voice_mixin.py` (start/stop/transcribe, enable/disable/status, read-aloud),
`hermes_cli/voice.py` and `hermes_cli/cli_tui_mixin.py` (record-key parsing and the key handler
only), `hermes_cli/config_defaults.py` (the `voice` defaults block), and
`website/docs/user-guide/features/voice-mode.md` (skim: CLI part). Closed before writing.
Evidence: none.

What it does (own words): in the classic REPL a person can talk instead of type. `/voice on`
(or `indus chat --voice`) checks that recording can work here — the audio library from the
`voice` extra imports, the PortAudio library loads, an input device exists, a speech-to-text
provider is ready — and turns voice mode on; any failure prints one plain sentence and voice
stays off. While on, the push-to-talk key (default Ctrl+Space, `voice.record_key`) starts a
16 kHz mono 16-bit take. The take ends when the key is let go, when the person has spoken and
then been quiet for `voice.silence_seconds` (2 s), when nobody spoke at all within
`voice.wait_seconds`, or at the hard cap `voice.max_seconds`. "Quiet" is a block whose RMS
level is under `voice.silence_threshold`. A take with no block above the threshold is dropped
("nothing heard"). Otherwise the WAV is transcribed with the P10-18 library off the prompt's
thread; the transcript is echoed and sent as a user turn exactly as if typed (so the busy-input
rule applies when a reply is running). An inaudible result says so and sends nothing. With
`voice.read_aloud: true` the final answer of each completed turn (the text after the last tool
call) is cleaned of markdown and spoken through the P10-17 speech engine, then played.
`/voice off` cancels a take in progress, stops playback and turns voice off; `/voice status`
shows on/off, the key, read-aloud and whether a take or a transcription is running.

Recording never runs while a card is open (approval, clarify, sudo, secret): the key is refused
with a note, and a card that arrives during a take cancels the take.

Terminals do not report key release. The binding reads repeated presses: a press that comes
within the repeat gap of the previous one means the key is being held (auto-repeat); once
repeats stop for longer than the release gap the key counts as let go. A single tap (no
repeats) starts a take that ends on the next tap or on silence.

Inputs → outputs: `/voice on|off|status`, `indus chat --voice`, the record key, settings
`voice.record_key`, `voice.silence_threshold`, `voice.silence_seconds`, `voice.wait_seconds`,
`voice.max_seconds`, `voice.read_aloud` → status lines, a WAV under `<home>/cache/audio/`
(deleted after a good transcript, kept and named after a failure), a user turn, a spoken reply.

Files: `src/indus/cli/voice.py`, `tests/unit/cli/test_voice.py`, this note. Shared (wiring):
`cli/slash/dispatch.py` (handler), `cli/slash/context.py` (`voice` seam), `cli/repl/app.py`
(extra key bindings and feed observers), `cli/launch.py`, `cli/chat.py`, `cli/args.py`,
`cli/handoff.py` (`--voice`), `foundation/config/schema.py` + test (`voice` section),
`locales/en.yaml` (`cli.voice.*`), `pyproject.toml` (`sounddevice` in the `voice` extra),
`tests/unit/service/commands/test_registry.py` (`voice` landed).

Edge cases / failure modes:
- extra missing (ImportError), PortAudio missing (OSError), no input device, STT not ready →
  one message, voice off; the device opening at key time fails → message, no take;
- the record key is bound only while voice is on (otherwise the key keeps its usual meaning);
  a start asked for while off → hint to run `/voice on`; while a transcription runs → "still
  working";
- card open at key time → refused; card opens during a take → take cancelled;
- take too short, or never 0.3 s of loud audio (a click or knock is not speech; a pause of
  0.3 s before that starts the count again) → "nothing heard", nothing sent;
- the key still held (auto-repeat) after silence ended the take → those presses do not start
  a new take; a fresh press later does (`test_a_key_still_held_after_a_silence_stop_...`);
- the device refuses 16 kHz → it records at its own default rate and the blocks are converted
  to 16 kHz (linear interpolation) before they reach the take;
- STT error → message, recording kept with its path; inaudible → message, nothing sent;
- `/voice off` during transcription → the late transcript is dropped;
- malformed config values (bools, strings, negatives) → defaults; an unusable record key
  (unknown name, Ctrl+C/D/J/M/Z and other keys the REPL owns) → default key plus a note;
- read-aloud: empty final text, failed or interrupted turn → nothing spoken; speech engine
  error → one note; a format other than WAV → note with the saved path (not played).

Our design differences / decisions:
- Default key Ctrl+Space (row decision), silence 2 s, threshold 300 RMS, no-speech wait 10 s,
  cap 120 s; config keys named by us.
- Release detection from auto-repeat (held) plus tap-to-stop, instead of a pure toggle.
- No continuous re-arming after a reply, no beeps, no level meter, no stop phrases, no
  hallucination filter, no streaming STT or TTS, no barge-in, no wake word (P10-19 "CLI flow
  only; defer streaming").
- Audio through `sounddevice` raw streams (bytes, no numpy); RMS in pure Python. The stream is
  opened and closed on a worker thread so a slow audio system never freezes the prompt.
- Playback only of WAV through the same library (Piper emits WAV; OpenAI is asked for WAV); no
  external player processes.
- Recording allowed while a reply runs: the transcript follows `display.busy_input_mode`.

Tests we will write (`tests/unit/cli/test_voice.py`, fake device, fake STT/TTS, fake clock):
- key down/up → transcript echoed and submitted as a user turn, WAV is 16 kHz mono;
- silence after speech auto-stops at 2 s on the fake clock (not at 1.9 s); no speech → stop at
  the wait limit with "nothing heard"; hard cap;
- held-key reading: repeats keep the take going, a gap ends it; tap mode;
- no device / missing extra / STT not ready → one message and off;
- card open → refused, no stream opened; card arriving mid-take → cancelled;
- read-aloud calls the fake TTS once with the final text only; off by default;
- `/voice on|off|status` and bad arguments; `--voice` flag reaches the launch;
- key parsing and config fallbacks; messages equal `locales/en.yaml` `cli.voice.*`.

## Decisions for the lead
- `sounddevice==0.5.6` (MIT, uploaded 2026-08-17, inside the 2026-10-04 lock window) added to
  the `voice` extra; it needs `cffi` (already locked, MIT-0 exception). Its macOS/Windows wheels
  bundle PortAudio (MIT). numpy is not needed by our code. Please re-lock.
- "Approval card" read as any card (approval, clarify, sudo, secret): a sudo or secret card
  being typed into must never be captured either.
- `SlashContext.voice` is a new optional seam (like `automation`); the TUI/gateway voice
  surfaces (P12/P15) do not use this module.

Compare checklist (filled after the rewrite; reopened `tools/voice_mode.py`,
`hermes_cli/cli_voice_mixin.py`, `hermes_cli/voice.py`):
- [x] 16 kHz mono int16 capture (test: `test_voice.py::test_the_take_is_16khz_mono_16bit`)
- [x] silence after speech ends the take; no-speech wait; hard cap (tests:
  `test_silence_after_speech_stops_the_take_at_two_seconds`,
  `test_nobody_speaking_stops_at_the_wait_limit_and_sends_nothing`,
  `test_hard_cap_ends_a_long_take`)
- [x] too-short / too-quiet take dropped before transcription
  (`test_a_quiet_take_released_early_is_dropped`)
- [x] double start prevented; stop allowed any time (`test_a_second_start_while_recording_does_nothing`)
- [x] no recording while a prompt card is open (`test_an_open_card_refuses_recording`,
  `test_record_key_in_the_repl_echoes_and_submits`)
- [x] requirements check names what is missing; voice stays off
  (`test_no_microphone_gives_one_message_and_voice_stays_off`,
  `test_sound_device_names_the_missing_extra`, `..._portaudio_and_a_missing_microphone`,
  `test_speech_to_text_not_ready_keeps_voice_off`)
- [x] transcript submitted as a user message; empty/inaudible not sent
  (`test_key_down_and_up_submits_the_transcript_as_a_user_turn`,
  `test_inaudible_transcript_sends_nothing`)
- [x] recording kept on failure (`test_failed_transcription_keeps_the_recording`)
- [x] read-aloud strips markdown/code/URLs, plays, failure is one note
  (`test_read_aloud_speaks_the_final_answer_once`, `test_read_aloud_failures_are_one_note`,
  `test_spoken_text`)
- [x] disable cancels the take and stops playback
  (`test_turning_off_drops_the_take_and_a_late_transcript`, `test_a_new_take_stops_playback`)
- [x] record key parsing refuses keys the prompt needs (`test_unusable_record_keys`)
- [x] gap: speech was any single loud block; the reference confirms speech only after a
  sustained stretch with short dips tolerated — fixed by the 0.3 s confirmation with a 0.3 s
  dip reset, pinned by `test_a_click_is_not_speech`
- [x] gap: the reference avoids blocking the prompt while the audio stream opens or closes —
  fixed by opening/closing on a worker thread, pinned by
  `test_a_take_ended_while_the_microphone_opens_closes_it`
- [x] gap: the reference records at the device's default rate — fixed our way: 16 kHz first,
  the device rate converted to 16 kHz when 16 kHz is refused, pinned by
  `test_sound_device_falls_back_to_the_device_rate`, `test_resampler_converts_48k_to_16k_across_blocks`
- [x] gap: Ctrl+L (clear screen) was accepted as a record key — now refused
- Deliberate differences: default key Ctrl+Space (reference: another Ctrl letter) and our own
  config names; silence 2 s and no-speech wait 10 s (row and our choice); a key press read
  as hold/release from auto-repeat plus tap/tap (reference: toggle only); a card arriving
  mid-take drops the take (reference only refuses to start); a take may start while a reply
  runs (the transcript follows the busy-input rule; the reference does not start then); the
  stream is opened per take (the reference keeps one open while voice is on, which keeps the
  microphone live between takes); the read-aloud text is the final answer after the last tool.
- Not taken: continuous re-arming after each reply and the three-empty-takes stop, start/stop
  beeps and the "thinking" sound, the live level meter, spoken and typed stop phrases, the
  hallucination phrase filter, streaming STT/TTS, barge-in and full-duplex listening, wake
  word, phone-OS recorder, SSH/container/WSL environment heuristics (the device query decides),
  system player processes and WSL playback bridge, the one-time retry of a timed-out stream
  open, the old-recordings sweep, TTS engine warm-up leases.

## Status

Done (2026-10-19). Shared files changed (wiring only): `cli/slash/dispatch.py`,
`cli/slash/context.py`, `cli/repl/app.py`, `cli/launch.py`, `cli/chat.py`, `cli/args.py`,
`cli/handoff.py`, `foundation/config/schema.py` + `test_schema.py`, `locales/en.yaml`,
`pyproject.toml` (+ `uv.lock` regenerated locally for the lead to re-lock),
`THIRD_PARTY_NOTICES.md`, `tests/unit/service/commands/test_registry.py`.
