# Task: Desktop pet and `/pet` (P24-19) Phase: 24 Owner: wave-39 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/pets.py` (all 427 lines:
subcommands, config helpers), `tui_gateway/contracts/projects_pets.py` (pet methods, lines 311–477),
`tui_gateway/contracts/billing_delegation_pets.py` (pet.generate / pet.hatch / pet.cancel rows),
`tui_gateway/methods_session.py` (pet.generate, pet.hatch handlers, lines 1700–1830),
`tui_gateway/server.py` (draft staging and the sprite payload helper, lines 1815–1960),
`agent/pet/constants.py` (geometry, scale bounds, state rows), `apps/desktop/src/components/pet/`
(file list; `roam-geometry.ts` and the head of `roam-behavior.ts` for behaviour only). Research 08
§3.21 (pet row: generate 420,000 ms, hatch 3,600,000 ms). Closed before writing.
Evidence: none.

What it does (own words): a small companion figure the person makes themselves. `indus pet
generate "<idea>"` asks the profile's configured image service (P10-16 `image_gen.*`) for up to four
looks using our own prompt; the drafts are kept in a staging folder for an hour. `indus pet hatch
<token> --pick N --name X` turns one draft into a pet: one picture per mood (our own six moods),
each drawn by the same service with the draft as its reference, saved under `<home>/pets/<slug>/`.
A pet is shown only when the person picks it (`indus pet use`), and `off` hides it. `show`, `list`,
`rename` and `remove` manage the pets. The daemon serves the same actions as `pet.*` methods for the
desktop and the TUI. On the desktop the pet lives in its own transparent overlay window over the
work area: clicks pass through everywhere except the figure itself; it wanders slowly inside the
work area, says short status lines in a bubble, and hides while a full-screen window is focused.
The TUI's `/pet` shows the pet's card in a pane.

Inputs → outputs:
- `indus pet generate IDEA [--count 1..4]` → a draft token and the draft files (exit 0); no image
  service / timeout (420 s) / nothing usable → message, exit 1.
- `indus pet hatch TOKEN --pick N --name NAME` → a new pet (not active), exit 0; an expired token,
  a bad pick, a timeout (3,600 s) → exit 1.
- `indus pet list|show [SLUG]|use SLUG|off|rename SLUG NAME|remove SLUG [-y]`.
- Settings `pet.enabled` (false), `pet.active` (""), `pet.scale` (1.0, clamped 0.25–3).
- `pet.*` rows (`PET_METHODS`): `pet.info`, `pet.list`, `pet.generate`, `pet.hatch`, `pet.select`,
  `pet.disable`, `pet.rename`, `pet.remove`; deadlines `pet.generate` 420 s, `pet.hatch` 3,600 s
  (`PET_DEADLINES`).
- Desktop IPC: `indus:pet:show` / `indus:pet:hide` / `indus:pet:say` (invoke/send from an app
  window), `indus:pet:pointer` (send from the pet window: over the figure or not), `indus:pet:state`
  (event to the pet window), `indus:pet:get` (invoke from the pet window).

Files: `src/indus/service/pets.py` (store + generation, new), `src/indus/service/pets_rpc.py`,
`src/indus/cli/commands/pet.py`, `tests/unit/cli/commands/test_pet.py`,
`tests/unit/service/test_pets_rpc.py` (new), `apps/desktop/electron/windows/petWindow.ts`,
`apps/desktop/electron/windows/petWindow.test.ts` (new), `apps/desktop/src/pet/PetOverlay.tsx`,
`apps/desktop/src/pet/roam.ts`, `apps/desktop/src/pet/roam.test.ts`, `apps/desktop/src/pet/strings.en.ts`
(new), `apps/tui/src/commands/pet.ts` (new). Shared: `cli/app.py` (registry row), the CLI contract
tables, `foundation/config/schema.py` (`pet` section), `windows/factory.ts`, `windows/manager.ts`,
`windows/windowUrl.ts`, `ipc/channels.ts`, `ipc/schemas.ts`, `src/global.d.ts` (generated),
`src/app/bridge.ts`, `src/app/App.tsx`, `src/i18n/en.ts`, `electron/main.ts`, TUI `localCommands.ts`.

Edge cases / failure modes:
- the image service is missing a key, or none is configured → "set image_gen.*" message, no draft
  folder left behind; a service that answers with a link and no bytes is refused (we never fetch an
  arbitrary URL here);
- bytes that are not PNG / JPEG / WebP are refused; at most 4 drafts, each ≤ 16 MiB;
- a timeout cancels the provider calls and removes the half-written folder;
- hatch: the token is checked as a hex id (no path tricks); an unknown pick; a name that slugs to
  nothing; a slug clash gets `-2`, `-3`…;
- remove/rename of the active pet: settings follow (remove turns the pet off);
- desktop: the pet window is never focusable by itself, never takes focus, has no preload access
  beyond the bridge, and every channel checks which window kind sent it;
- roam: work area smaller than the figure → the figure sits at the top-left corner; a display
  change re-clamps.

Our design differences / decisions:
- No gallery and no downloads: every pet is made by the person (D-16.23, no reference art). The
  reference ships a public pet gallery and sprite sheets; we keep one picture per mood, animated by
  the renderer (a gentle bob), so no sprite-sheet atlas or image library is needed.
- Our own mood names: `rest`, `walk`, `talk`, `think`, `sleep`, `cheer`.
- The pet window covers the work area and is click-through except over the figure (the renderer
  reports when the pointer is over it); the reference roams inside the app window.
- Fullscreen: main hides the pet while one of our windows is full screen and focused; on macOS the
  window is not visible on full-screen spaces, so the OS hides it over other apps' full screens.
  Other apps' full screens on Linux/Windows are not detected (recorded as a follow-up).
- `pet.*` method rows live beside their handlers (as `projects.*` in w37-B1); adding them to the
  method catalogue and the TS codegen is a wiring step.

Tests we will write:
- Python: generate with a fake image provider (drafts written, count clamp, bad bytes refused);
  the 420 s generate timeout honoured (shortened through the parameter and the constant checked);
  hatch with a fake provider (six moods, reference passed, not active); rename/remove follow the
  active pet; CLI non-TTY rows; rpc handlers over a temp home.
- vitest: roam stays inside the work area for random geometries (seeded); pet window controller:
  pointer toggles click-through, fullscreen hides, sender kind checks; the pet window passes the
  registry's sender checks.

## Decisions for the lead
- Pet pictures: one image per mood instead of a sprite sheet (no Pillow); change by adding a sheet
  composer later.
- `use`/`off`/`list` were added beside the row's generate/hatch/show/rename/remove (the desktop
  needs a way to choose the shown pet).
- `pet.*` rows are not in `indus.protocol.methods` yet (wiring).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] generate count clamp 1..4, an idea required (test: `tests/unit/cli/commands/test_pet.py::test_generate_writes_drafts_with_our_prompt`, `::test_generate_without_an_idea_or_a_service`)
- [x] generate / hatch deadlines 420 s / 3,600 s (tests: `::test_deadlines_are_the_rows_numbers`, `::test_generate_uses_the_420_s_deadline_by_default`, `::test_generate_timeout_is_honoured_and_leaves_nothing`, `::test_hatch_timeout_and_bad_tokens`)
- [x] hatch: the new pet is installed but not shown; `use` / `pet.select` adopts (test: `::test_hatch_draws_every_mood_after_the_draft`)
- [x] remove of the shown pet turns the display off; rename follows the active slug (test: `::test_use_rename_remove_follow_the_shown_pet`)
- [x] scale clamped to bounds, one setting for every surface (test: `::test_scale_is_clamped`; reference 0.1-3, ours 0.25-3)
- [x] drafts swept after an hour (test: `::test_drafts_are_swept_after_an_hour`)
- [x] `pet.info` sends the pictures only when the caller's revision is stale (test: `tests/unit/service/test_pets_rpc.py::test_generate_hatch_select_info_round_trip`)
- [x] overlay: click-through except the figure, never focused, hidden under a focused full-screen window, closes with the last app window, sender kinds checked (tests: `apps/desktop/electron/windows/petWindow.test.ts`); roam inside the work area (`apps/desktop/src/pet/roam.test.ts`)
- Deliberate differences: no gallery or downloads (every pet is generated by the person, D-16.23); one picture per mood instead of a sprite-sheet atlas; our own mood names; the overlay covers the work area rather than roaming inside the app window; drawing deadlines instead of cancel tokens.
- Not taken: gallery, export/import zip, thumbnails, reference-image upload for generate, `pet.generate.status` (availability probe; the desktop learns it from the error), kitty/sixel terminal rendering, `doctor`.

## Status

Done (2026-10-21). Wiring owed (lead/integrator): add `PET_METHODS` to `indus.protocol.methods` + TS codegen, `pets_rpc.register(table)` in the daemon's dispatcher, merge `PET_DEADLINES` into `rpc.dispatch.DEADLINES`; then `PetHost` / TUI `/pet` call typed methods instead of the loose call. Shared files touched: `cli/app.py`, CLI contract tables, `foundation/config/schema.py` (`pet`), window factory/manager/url + factory snapshot, IPC table + schemas + generated `global.d.ts`, `bridge.ts`, `App.tsx`, `main.ts`, `en.ts`, TUI `localCommands.ts`.

## Integration (P24 integrator, 2026-10-21)

Wired: `pet.*` declared in `indus.protocol.pets` (codegen), served by the daemon's WebSocket and the
TUI bridge; the drawing deadlines (`PET_DEADLINES`, 435 s / 3615 s) are in the dispatcher's table
and the desktop client's (`gateway/timing.ts`).
