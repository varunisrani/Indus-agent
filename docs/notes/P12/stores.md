# Task: renderer stores and destination guard (P12-09) Phase: 12 Owner: wave-8 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/app/uiStore.ts` (the
default values), `ui-tui/src/app/turnStore.ts` (per-turn fields, reset), `ui-tui/src/app/overlayStore.ts`
(which overlays block input, which survive a turn's idle edge). Research `05-tui.md` §3.27, §3.30,
traps R05.10, R05.18. Wave-8 contract (shared interfaces). Evidence: none.

What it does (own words): three small observable stores hold everything the renderer paints that
is not composer-local. `ui` is the session-level state (which session, live info, busy, status
text, display preferences with their TUI defaults). `turn` is what the running turn has produced so
far (stream buffer, sealed segments, tools, trail, activity, reasoning, todos, outcome). `overlay`
holds at most one blocking card plus independent full-pane views; while anything that owns the
keyboard is open the composer is "blocked". Every async action captures a destination
`{sid, storedSid, revision}` before it starts and checks it when the result lands, so a reply that
returns after a session switch is dropped instead of painted into the wrong chat.

Inputs → outputs: `uiStore.getState().patch({...})`, `turnStore.getState().patch/reset()`,
`overlayStore.getState().open(card)/close(id)/blocked()` → React hooks `useUi`, `useTurn`,
`useOverlay` re-render on change; `captureDestination()` / `isCurrent(dest)`.

Files: `apps/tui/src/state/{uiStore,turnStore,overlayStore,destination,transcriptStore}.ts`,
`apps/tui/src/state/stores.test.ts`.

Edge cases / failure modes:
- a config without a busy-input mode keeps `queue` (R05.18); an unknown value falls back to it.
- a second blocking card while one is open waits in a FIFO instead of replacing the first (two
  questions at once would otherwise lose one answer); closing the open card shows the next.
- `close(id)` of a card that is not open removes it from the waiting list (a withdrawn request).
- turn idle closes the flow cards of the finished turn (approval, clarify, confirm, sudo, secret,
  pager) but keeps the pickers the user opened (model picker, session picker) and the views.
- a session switch bumps `revision`, so even a switch back to the same id invalidates old results.
- trail and activity never grow past 8 entries.

Our design differences / decisions:
- zustand vanilla stores (one per concern) instead of the reference's atom library; the field
  names follow the wave-8 contract, not the reference.
- the finished conversation lives in a fourth store, `transcriptStore` (messages appended by the
  turn controller and the event handler); the reference keeps it in React state of the app.
- pickers are cards (`modelPicker`, `sessionPicker`) per the contract; "views" are input-owning
  full-pane views named by string.
- `bumpRevision()` lives next to the guard in `destination.ts`.

Tests we will write: defaults table; busy-mode default with and without the config key; late result
dropped after a session switch (and after switching back to the same id); idle clears approval and
keeps the model picker; card FIFO; blocked() for cards and views; trail/activity caps; reset.

## Decisions for the lead
- Additions to the contract interfaces (names kept, fields added): `UiState.notice`,
  `UiState.compacting`, `UiState.title`, `UiState.connected`; `ToolRow.depth?` (subagent indent, lead request), diff segment ids start with `diff`; `TurnState.reasoningActive`,
  `TurnState.turnId`, `TurnState.generation`; `Card.turnId?`; `OverlayState.waiting`,
  `openView/closeView/closeFlowCards/closeByRequest`; new `transcriptStore` (R2 renders it).
- `busyInputModeFrom(raw)` normalises a config value (unknown → `queue`); config sync (P12-26)
  should use it.

Compare checklist (filled after the rewrite; reopened the three files above):
- [x] defaults: busy `queue`, details `collapsed`, status bar `top`, streaming on, reasoning hidden,
  inline diffs on, timestamps off, paste 5 / 2,000 (test: `stores.test.ts::starts from the TUI defaults`)
- [x] blocking set = any input-owning overlay (test: `…::is blocked by an input-owning card or any view`)
- [x] idle keeps user-opened pickers, drops flow cards (test: `…::closes the finished turn's flow cards…`)
- [x] destination captured per action, mismatch dropped (test: `…::drops a late result after a session switch`)
- Deliberate differences: the reference resets the whole overlay object and re-copies the kept
  keys; ours keeps one card slot plus a waiting list, so a second question never overwrites a first.
  The reference's guard also compares an execution epoch/generation (canonical daemon); our stdio
  bridge has none, so `{sid, storedSid, revision}` is the whole guard.
- Not taken: battery, focus view, pet, voice, live-session count, background task set (later phases).

## Status

Done (2026-10-19). Files above, `stores.test.ts` (15). Shared: none.
