# Task: Wake word (P24-20) Phase: 24 Owner: wave-39 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/methods_voice.py`
(`wake.*` handlers and the owner lease only, lines 318–620), `tools/wake_word.py` (constants, the
detector's feed / fire / cooldown, the start/stop/pause/resume surface — skimmed for behaviour),
`apps/desktop/src/store/wake-word.ts` (behaviour only: start timeout, refusal texts, status sync),
`apps/desktop/src/app/wake-indicator/` (file list; the indicator app), `apps/desktop/electron/
wake-indicator.ts` (states and placement). Research 08 §3.21 (start timeout 180,000 ms). Closed
before writing.
Evidence: none.

What it does (own words): a hands-free way to start talking to the agent. When the person has
turned it on (an explicit gesture that also records their consent), the daemon listens to the
microphone for the phrase "hey indus" with a local keyword engine. A detection fires once per
utterance — several frames over the threshold in a row, then nothing until the score falls back
and a 2 s cooldown passes — and hands off to voice mode (P10): the owning surface gets a
`wake.detected` event and opens its own capture. The microphone is opened only while the feature
is enabled, consented and started, and is released on pause (while the surface records), stop, or
when the owner goes away. One surface owns the listener at a time. The desktop shows a small
always-on-top indicator window (listening / heard / capturing) whenever the microphone is in use,
and Settings has the toggle; the TUI has `/wake on|off|status`.

Inputs → outputs:
- settings `wake_word.enabled` (false), `wake_word.consent` (false; written only by an explicit
  on), `wake_word.engine` (`openwakeword`, `sherpa`), `wake_word.phrase` ("hey indus"),
  `wake_word.model` (path to a model the person trained or supplied; none is shipped),
  `wake_word.threshold` (0.5), `wake_word.confirm_frames` (3), `wake_word.cooldown_s` (2.0).
- `wake.start {surface, persist?, consent?}` → `{started, reason?, phrase, engine}`; refusals
  `disabled`, `consent_required`, `unavailable` (with a hint), `owned`;
  `wake.stop {persist?}`, `wake.pause`, `wake.resume`, `wake.status`, `wake.feed {pcm}`
  (client capture, base64 int16 mono 16 kHz, ≤ 64,000 bytes); event `wake.detected {phrase,
  surface}`. Deadline `wake.start` 180 s (`WAKE_DEADLINES`): the first start may install or load a
  model.
- Desktop: `indus:wake:indicator` (send from an app window `{state}`), `indus:wake:indicator-state`
  (event to the indicator), `indus:wake:indicator-get` (invoke from the indicator).

Files: `src/indus/tools/builtin/media/wake_word.py`, `src/indus/service/wake_rpc.py`,
`tests/unit/tools/builtin/media/test_wake_word.py`, `tests/unit/service/test_wake_rpc.py` (new),
`apps/desktop/electron/windows/wakeIndicator.ts`, `apps/desktop/electron/windows/wakeIndicator.test.ts`
(new), `apps/desktop/src/wake/WakeIndicator.tsx`, `apps/desktop/src/wake/WakeIndicator.test.tsx`,
`apps/desktop/src/wake/strings.en.ts` (new), `apps/tui/src/commands/wake.ts`,
`apps/tui/src/commands/wake.test.ts` (new). Shared: `foundation/config/schema.py` (`wake_word`),
`pyproject.toml` (extra), window factory/manager/url, IPC table, `main.ts`, `en.ts`, TUI
`localCommands.ts`.

Edge cases / failure modes:
- disabled or no consent → the microphone is never opened (spied in tests);
- engine missing (package not installed) or no model file → `unavailable` with what to install /
  configure; nothing persisted;
- a second surface starting while one owns it → `owned`; a dead owner is released first;
- a frame of the wrong length is padded or split; an over-long `wake.feed` is refused;
- a detection while paused is ignored; pause twice / resume without pause are no-ops;
- the start answer takes up to 180 s on first use; the desktop waits that long, then shows a
  timeout and leaves the toggle off;
- the indicator never shows content of the audio, only the state.

Our design differences / decisions:
- Phrase "hey indus" (glossary (e)10); no model ships — the person trains one with the engine's own
  tooling or supplies one; the engine is unavailable until a model path is set.
- Consent is a stored, explicit yes (`wake_word.consent`), separate from `enabled`: an auto-arm at
  startup never opens the microphone without it.
- One detector state machine for every engine (scores in, one fire per utterance out), so engines
  are thin adapters.
- Engines: openWakeWord (default per D-16.52) and sherpa-onnx keyword spotting; Porcupine left out
  (its runtime needs a vendor access key and ships under the vendor's own terms — see below).

Tests we will write:
- fake engine + fake audio frames: one detection per utterance; confirm frames; cooldown; re-arm
  after silence; disabled → no microphone open (spy); consent missing → no open; pause releases
  the microphone; owner lease and refusals; rpc start/stop/status over a fake service; feed limits.
- vitest: indicator start timeout at 180 s (fake timers); indicator states; controller applies
  start/stop answers; indicator window placement and sender checks.
- TUI: `/wake on|off|status` call the right methods.

## Decisions for the lead
- openWakeWord 0.6.0 (Apache-2.0, 2024-02-11) cannot be locked on Python 3.13: on Linux it requires
  `tflite-runtime`, which has no 3.13 wheels. The engine adapter is built and used when the package
  is importable, but no `wake` extra is added; options: wait for a release without tflite, or make
  sherpa-onnx the default engine.
- `wake-sherpa = ["sherpa-onnx==1.13.8"]` (Apache-2.0, 2026-09-10, one dependency
  `sherpa-onnx-core` 1.13.8 Apache-2.0) is added as an extra.
- Porcupine (`pvporcupine` 4.0.3, Apache-2.0 SDK) left out: the engine needs a Picovoice access key
  and its runtime library and keyword files are under Picovoice's terms; record under §2.8 if the
  owner agrees.
- `wake.*` rows are not in the method catalogue yet (wiring).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] start refusals disabled / unavailable / owned, and an "on" that cannot arm stores nothing (tests: `tests/unit/service/test_wake_rpc.py::test_auto_arm_without_consent_refuses_and_writes_nothing`, `::test_an_on_that_cannot_arm_stores_nothing`, `::test_other_connections_are_not_owners`)
- [x] stop with persist turns enabled off; pause / resume owner-gated with `not_owner` (tests: `::test_explicit_on_stores_consent_then_detection_is_emitted`, `tests/unit/tools/builtin/media/test_wake_word.py::test_owner_lease_refusals`)
- [x] cooldown 2 s; confirmation frames 3; one fire per utterance (tests: `::test_one_detection_per_utterance`, `::test_confirmation_frames_reject_a_stray_spike`, `::test_cooldown_and_re_arm`)
- [x] feed cap 64,000 bytes, 16 kHz only, base64 (tests: `::test_client_capture_feeds_without_a_microphone`, `test_wake_rpc.py::test_feed_checks`)
- [x] disabled / no consent → no microphone open (tests: `::test_disabled_never_opens_the_microphone`, `::test_no_consent_never_opens_the_microphone`)
- [x] desktop start waits 180 s; indicator states (tests: `apps/desktop/src/wake/WakeIndicator.test.tsx`, `apps/desktop/electron/windows/wakeIndicator.test.ts`)
- Deliberate differences: consent is its own stored setting written only by an explicit on (the reference persists `enabled` on a gesture); the indicator also shows plain listening (the reference shows only detected / capturing) so the microphone is never in use without a visible light; no model ships.
- Not taken: `wake_word.surface` scoping (`disabled_for_surface`), multi-phrase profile enrolment, input-device listing, silent-microphone hints, the busy-microphone resume retry, a machine-wide lock across processes (one daemon per profile owns it here).

## Status

Done (2026-10-21). Wiring owed: `WAKE_METHODS` into the method catalogue + codegen, one `WakeRpc` per daemon with `emit` = the connection's event send and `connection_closed` on disconnect, `WAKE_DEADLINES` into the dispatcher; `wake.detected` → TUI voice mode (P10) and desktop `WakeController.attach(connection.onEvent)` with `handOff` = P24-35's dictation; mount `WakeToggle` in the voice settings view (U3, P24-35); the TUI `/wake on` uses the channel's default call deadline until the RPC context takes a per-call timeout. Shared: `pyproject.toml` (`wake-sherpa` extra), config schema (`wake_word`), window factory/manager/url, IPC table, `main.ts`, `en.ts`, TUI `localCommands.ts`.

## Integration (P24 integrator, 2026-10-21)

Wired: `wake.*` and the `wake.detected` notification are declared in `indus.protocol.wake`
(codegen), served by the daemon's WebSocket (one `WakeRpc` per daemon; a detection is sent from the
audio thread to the connection that armed it, a closing connection releases its lease) and by the
TUI bridge (one per bridge process). Desktop: `src/wake/appWake.ts` runs one controller per main
window over the foreground connection, armed at launch without consent (stored consent only); a
detection asks the composer on screen to dictate (`indus-wake-dictate`, taken by `VoiceButtons`) and
the end of that dictation resumes listening; with no composer on screen it listens again at once.
Settings › Chat › Voice holds `WakeToggle`. Client deadline 190 s for `wake.start`
(`gateway/timing.ts`). Owed: TUI `wake.detected` → voice mode and its 180 s deadline; the engine
default (owner, D-16.800).

## Wiring (wave 42, WC, D-16.799)

Read: none (wiring).

- TUI `wake.detected` → voice: `apps/tui/src/commands/wakeHandOff.ts`, wired in `AppRuntime`. On a
  detection it says so, shows "listening…", asks for one take with `voice.listen` (new, below),
  sends the transcript as the user's prompt (the submitter's normal path: echoed, queued while a
  reply runs) or says why nothing was sent, and always calls `wake.resume`. Nothing is recorded
  while a question card is open; a second detection during a take is ignored.
- `voice.listen` (declared in `indus.protocol.voice`, codegen regenerated; handler
  `service.voice_rpc.voice_listen`, served by the bridge and the daemon): one take on the
  answering process's microphone — the one the wake word used and a detection released — with the
  REPL voice mode's rules, then the profile's speech-to-text. The take rules moved, unchanged, from
  `cli/voice.py` to `tools/builtin/media/voice_take.py` (`VoiceSettings`, `Capture`, `level`, new
  `record_take`) so the service layer can use them; `cli.voice` re-exports them. Deadline 3,900 s
  (the longest `voice.max_seconds` plus the transcription).
- `/wake on` now waits 190 s (`Rpc.call(..., {timeoutMs})`, new optional third argument).
- Engine default per the D-16.800 recommendation: `wake_word.engine` defaults to `sherpa`
  (`DEFAULT_ENGINE`); `openwakeword` stays selectable where it installs.
Tests: `tests/unit/service/test_voice_rpc.py::test_wake_detection_hands_the_microphone_to_one_take_then_listens_again`
(real `WakeRpc`/`WakeService` + `voice_listen` on one scripted device: wake stream, take stream,
wake stream again, never two open), `::test_listen_*`; `apps/tui/src/commands/wakeHandOff.test.ts`
(the runtime over the fake bridge: detection → `voice.listen` → `prompt.submit` → `wake.resume`).

Decisions for the lead (wiring): `voice.listen` is a new declared method (the terminal has no
microphone of its own; the reference's TUI records in its bridge too); D-16.800's recommendation
applied (default engine sherpa-onnx) — the owner row can be closed or reversed with one line.

Status (wave 42): TUI hand-off and its deadline wired; engine default set.
