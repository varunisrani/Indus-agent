# Task: Model picker and status bar (P20-23) Phase: 20 Owner: wave-34 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/chat/composer/`
(model parts: `model-pill.tsx` and `reasoning-pill.tsx` names and sizes only — skim). Research 08 §3.13
(model picker, status bar). Closed before writing.
Evidence: none.

What it does (own words):
- **Model picker** (pill in the composer showing model and effort). The list comes from
  `model.options` (providers; each may carry a `models` list). Search ignores case and separators
  (`gpt4o` finds `gpt-4o`, `claude sonnet` finds `claude-sonnet-…`). Favourites (star) sort first. Each
  model has its own effort and fast presets, applied when that model is picked. A pick applies to the
  **session** (`config.set { key: "model", value: "provider:model", session_id }`) and becomes this
  device's sticky choice (localStorage, per profile), which new chats follow. It never writes the
  profile default — except the very first pick on a profile that has none (`config.get { key:
  "model" }` without a session is empty), which also writes the default once. A risky switch asks
  first: a provider change in a session that already has messages, or the daemon's
  `CONFIRM_REQUIRED`; confirming repeats the call with `confirm: true`.
- **Status bar** (bottom of the shell): per-session yolo toggle (`config.set yolo`, tooltip says the
  hard floors still apply), context meter (`context_tokens / context_window`) with a breakdown
  popover (input, output, cache read/write, reasoning), optional cache-hit rate and tokens/s (mean of
  the last 10 model calls, from `session.usage` deltas), workspace (`cwd`), model, open approvals,
  turn timer, backend version, and a daemon menu with Reconnect. Items can be hidden or shown from a
  customise menu; the choice is saved per device.

Inputs → outputs: `model.options`, `config.get/set`, session events (`session.info`, `session.usage`,
`turn.started/done`), open approvals → pill, popover, bar items; picks → `config.set`.

Files: `apps/desktop/src/chat/ModelPicker.tsx`, `apps/desktop/src/app/StatusBar.tsx`,
`apps/desktop/src/chat/ModelPicker.test.tsx`, `apps/desktop/src/app/StatusBar.test.tsx`.

Edge cases / failure modes:
- `model.options` fails: the pill shows the session's model, the list shows the error with Retry;
- no session yet (fresh draft): the pick is only stored as sticky and applied at the first send;
- deferred switch (`applied: "deferred"`): pill notes "after this reply";
- usage without a context window: meter hidden, breakdown still available.

Our design differences / decisions:
- risky = provider change with history, or server `CONFIRM_REQUIRED` (no static risky list).

Tests we will write:
- first pick on an empty profile writes the default once; later picks never; search `gpt4o` finds
  `gpt-4o`; risky model shows confirm and repeats with `confirm: true`; presets applied; favourites first;
- status bar: yolo toggle per session; context meter + popover; tokens/s over the last 10 calls;
  customise hides an item; Reconnect calls the store.

## Decisions for the lead
- Sticky key `indus.desktop.model.<profile>`; preset key `indus.desktop.model-presets`; favourites
  `indus.desktop.model-favourites`; first-pick flag `indus.desktop.model-default-checked.<profile>`;
  status items `indus.desktop.statusbar.items` (P20-26 registers these as profile-keyed storage
  families). The reference also keys the sticky choice by connection (remote vs local); ours by
  profile only until P20-26's registry exists — follow-up.
- The profile default is read with `config.get { key: "model.default" }` (empty when unset) and
  written with `config.set { key: "model" }` without a session.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Profile default written only on the first pick of a profile without one (tests:
  `ModelPicker.test.tsx::the first pick on a profile without a default writes it once; later picks
  never`, `::a profile that has a default is never written`).
- [x] Separator-insensitive search (tests: `::ignores separators and case`, `::search gpt4o lists gpt-4o`).
- [x] Risky switch asks first (tests: `::risky model shows the confirm and switches on Switch`,
  `::another provider mid-conversation asks before any call…`).
- [x] Presets and favourites (tests: `::applies the model's effort and fast presets`, `::starring a model keeps it first`).
- [x] Status bar: yolo per session, context popover, tokens/s window, items customisable, Reconnect
  (`StatusBar.test.tsx`, 7 tests).
- Deliberate differences: no reasoning clamp display (our sessions report the effort in effect).
- Not taken: terminal status item (P20-31), status bar toggle shortcut (P20-27 keybindings).

## Status

Done (2026-10-21).
