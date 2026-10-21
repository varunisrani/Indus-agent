# Task: Desktop voice (P24-35) Phase: 24 Owner: wave-39 builder U3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/chat/composer/hooks/`
(file list of the `use-voice-*` hooks; `use-composer-voice.ts` head for the responsibilities —
behaviour only), `tui_gateway/methods_voice.py` (method names and events: `voice.record`,
`voice.tts`, `voice.transcript`, `voice.status`, `voice.interrupted`; skim). Research 08 §3.13
(voice bullet). Closed before writing.
Evidence: none.

What it does (own words): three voice features in the composer.

1. **Dictation.** The mic button starts a capture; pressing it again (or Enter while capturing)
   stops it; the recorded audio goes to the daemon's speech-to-text method and the transcript is
   inserted into the draft — of the session the capture **started for**. The capture pins its
   recipient (session id + profile) when it starts; a session switch mid-capture does not move the
   text: it lands in the original session's draft (R08.19). Transcription has a timeout, default
   60 s (`voice.stt_timeout_s` overrides); on timeout the capture ends with "transcription timed
   out" and nothing is inserted. A newer capture makes an older pending result stale (dropped).
2. **Read-aloud.** A reply can be spoken: the text (markdown stripped) goes to the daemon's TTS
   method; the audio plays through one player; starting another read-aloud stops the first.
3. **Spoken conversation.** A loop: listen → voice-activity detection ends the turn after a
   silence (energy over a threshold for speech, 900 ms below it ends it) → transcribe → submit to
   the pinned session → speak the reply → listen again. **Barge-in**: speech detected while a reply
   plays stops the playback at once and starts a new listening turn. Leaving the loop stops capture
   and playback.

Microphone permission is asked only on first use (the first `getUserMedia` call happens when the
person presses the mic or starts a conversation — never at startup); a refusal shows "microphone
not allowed" and nothing records. While capturing, a visible indicator is on (the button's pressed
state + a "Listening" label); no capture runs without it.

Inputs → outputs: mic audio → `VoicePort.transcribe(audio, {signal})` → text into the pinned
draft; reply text → `VoicePort.speak(text)` → audio played; VAD frames → turn boundaries.

Files: `apps/desktop/src/chat/voice/dictation.ts` (engine, `browserRecorder`, `rpcVoicePort`),
`dictation.test.ts`, `conversation.ts` (`ReadAloud`, `Conversation`, VAD), `conversation.test.ts`,
`VoiceButtons.tsx` (+ test: pinned draft sink, `voiceTurn`), `strings.en.ts`. Stand-in:
`apps/desktop/src/test/_stubs_u3.ts` (the assumed daemon voice methods + `FakeVoice`). Shared:
`apps/desktop/src/chat/composer/Composer.tsx` (`voice` prop → the buttons), `src/i18n/en.ts`.

Edge cases / failure modes:
- stop pressed before any audio → nothing sent;
- empty transcript → nothing inserted, a quiet "nothing heard";
- STT error → message, the draft unchanged;
- the pinned session is gone when the text arrives → the text goes to that session's stored draft
  (the composer adopts it if the session comes back) — never to the current one;
- barge-in during the TTS request (before playback starts) → the request is abandoned;
- the composer unmounts mid-capture → tracks stopped, recorder released.

Our design differences / decisions:
- The engine is plain TypeScript (state machines with injected recorder, clock, VAD frames and
  player) so tests run without a browser; the React part only wires buttons.
- Voice methods: the daemon has none yet. We code against a `VoicePort` (`transcribe`, `speak`)
  and the stand-in describes the assumed RPC: `voice.transcribe {session_id, audio_b64, mime}` →
  `{text}`, `voice.speak {text}` → `{audio_b64, mime}`.
- Wake word is P24-20 (U1), not here.

Tests we will write:
- dictation: session switch during capture still delivers to the original session; STT timeout at
  60 s (fake timers); a stale result after a newer capture is dropped; permission denied path;
  mic asked only on first use.
- conversation: VAD ends a turn after silence; reply spoken; barge-in stops playback; leaving stops
  everything; turn submits to the pinned session.

## Decisions for the lead
- Wiring owed: pass `voice={rpcVoicePort((method, params) => connection.request(method, params))}`
  to the Composer from `chatSlots` once the daemon has `voice.transcribe` / `voice.speak` (shape in
  `_stubs_u3.ts`); the buttons stay hidden until then. A read-aloud button on replies (Transcript)
  over `ReadAloud` is a follow-up.
- Wire `VoicePort` to the daemon once voice RPC methods exist (P10's voice pipeline exposes them on
  the protocol); until then the buttons render only when a port is provided.
- VAD constants: speech threshold RMS 0.02, end of turn after 900 ms of silence, max turn 60 s.

Compare checklist (filled after the rewrite; reopened the hooks list and `methods_voice.py`):
- [x] recipient pinned at capture start; a switch mid-capture delivers to the original session (tests: `dictation.test.ts::a session switch during capture…`, `VoiceButtons.test.tsx::dictated words land in the pinned session's draft`)
- [x] timeout default 60 s, configurable (tests: `::transcription times out at 60 s…`, `::the configured deadline…`)
- [x] stale result after a newer capture dropped (test: `::a newer capture makes an older pending result stale`)
- [x] mic asked only on first use; denial records nothing (tests: `::asks for the microphone only on first use`, `::a denied microphone records nothing`)
- [x] barge-in stops playback, also while the audio is being made (tests: `conversation.test.ts::barge-in…`)
- [x] VAD turn end after silence, max turn, reply spoken, back to listening (tests: `conversation.test.ts`)
- Deliberate differences: engines are plain TS with injected recorder/levels/player; client-direct
  RPC shape is our stand-in.
- Not taken: full-duplex server-side listener, stop phrases, auto-speak of every reply, haptics,
  wake-word handoff (P24-20).

## Status

Done (2026-10-21) for the engines and buttons; the daemon voice methods are a stand-in (wiring
owed, above).

## Integration (P24 integrator, 2026-10-21)

The daemon has the two methods now (`indus.service.voice_rpc`, declared in `indus.protocol.voice`,
D-16.788): the clip goes through a 0600 file under `<home>/cache/voice/` to the profile's
speech-to-text provider and is removed; speech comes from the text-to-speech provider and its file
is removed after it is read; a missing provider is `PROVIDER_UNAVAILABLE`. `chatSlots` passes
`rpcVoicePort` over the connection to every composer, so the mic and talk buttons show. The stand-in
file became `src/test/fakeVoice.ts` (the tests' port, no interface stand-in left). The wake word's
hand-off starts this dictation (see `wake-word.md`).
