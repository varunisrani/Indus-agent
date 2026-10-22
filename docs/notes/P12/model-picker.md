# Task: Model picker (P12-23) Phase: 12 Owner: wave-8 builder R4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/components/modelPicker.tsx`
(stages, keys, key entry, disconnect, rows), test names of `ui-tui/src/__tests__/modelPicker.test.ts`
and `modelPickerReasoning.test.ts`, the `/model` entry of `ui-tui/src/app/slash/commands/session.ts`.
Ours: `docs/notes/P12/bridge-methods.md` (`model.options`, `model.save_key`, `model.disconnect`,
`config.set` model with `deferred` and `CONFIRM_REQUIRED`). Evidence: none.

What it does (own words): `/model` with no argument (or Ctrl+O) opens a picker over the transcript
without touching what is typed in the composer. Stage one lists providers (signed-in mark, name,
"no key" for those without one); stage two lists the provider's models; stage three offers a
reasoning effort (levels, off, keep current). Typing filters the active list; Esc first clears the
filter, then goes back a stage, then closes. Enter on a provider without a key opens the key stage
(masked entry saved with `model.save_key`); Ctrl+D on a signed-in provider opens a disconnect
confirmation (`model.disconnect`). Twelve rows are visible at a time. The footer shows the scope
("session only" or "global", Ctrl+G toggles it where allowed). Applying sends `config.set {key:
model, value: provider:model, session_id}`; a deferred reply (a turn is running) adds a notice that
the switch happens after the reply; a `CONFIRM_REQUIRED` refusal opens a danger confirm and retries
with `confirm: true`. `/model --refresh` reloads the options; `/model <spec>` switches directly.

Inputs → outputs: keys → reducer state + effects (`apply`, `saveKey`, `disconnect`, `close`);
`model.options {session_id?}` → rows.

Files: `apps/tui/src/pickers/modelPickerState.ts`, `apps/tui/src/pickers/ModelPicker.tsx`, tests
`modelPickerState.test.ts`, `ModelPicker.test.tsx`.

Edge cases / failure modes:
- options call fails → error line, Esc closes.
- a provider row with no model list → the model stage takes the typed filter text as the model id.
- filter with no matches → "no match" row; Enter does nothing (provider) or applies typed text (model).
- key entry: empty Enter ignored; save failure shows the error and stays in the stage; the key is
  held in a buffer outside React state and stores, cleared after the call.
- selection index clamps when the filtered list shrinks.

Our design differences / decisions:
- `model.options` takes only `session_id` in our protocol (extra keys are refused), so `--refresh`
  re-fetches the same call and `include_unconfigured` is not sent.
- our `ProviderOption` has no model list; the picker reads an optional `models: string[]` when the
  bridge adds one and otherwise lets the person type the model id.
- `q` types into the filter; only Esc closes (no collision with filter text).
- Ctrl+K on a signed-in provider re-opens the key stage (replace a key).
- global scope: the bridge's `config.set model` is per session only, so the picker is created with
  `allowGlobal: false` and the footer says "session only".

Tests we will write: every key per stage in the reducer; the apply flow with a fake RPC
(`deferred` notice, confirm round-trip, cancel of the confirm, reasoning follow-up); a rendered frame
with 12 rows; draft unchanged after open/close through `/model`.

## Decisions for the lead
- Ctrl+O belongs to R3's keymap; it should open the card exactly as `/model` does
  (`overlay.open({kind: "modelPicker", payload: {sessionId, refresh: false}, ownsInput: true})`).
- Bridge follow-ups: `ProviderOption.models`, a `refresh` flag on `model.options`, and a global
  scope for `config.set model`.

Compare checklist (filled after the rewrite; reopened `modelPicker.tsx`):
- [x] stages and keys per stage (test: `apps/tui/src/pickers/modelPickerState.test.ts` — provider,
  model, effort, key, disconnect, loading groups)
- [x] Esc clears the filter, then goes back, then closes; selection kept on the same provider (test:
  `modelPickerState.test.ts::Esc clears the filter first, then closes`, `::Esc clears the filter, then goes back to providers on the same row`)
- [x] key entry kept out of state; saved key opens models; failure noted (test:
  `modelPickerState.test.ts::typed text goes to the buffer, never the state`, `::a saved key opens the models; a failure stays with a note`)
- [x] 12 visible rows (test: `apps/tui/src/pickers/ModelPicker.test.tsx::shows 12 provider rows and a count of the rest`)
- [x] deferred reply notice; confirm_required round trip; declined confirm (test:
  `ModelPicker.test.tsx::walks provider → model → effort and applies; a deferred switch leaves a notice`,
  `::confirm_required round-trips through the confirm hook`, `modelPickerState.test.ts::a declined confirmation changes nothing`)
- [x] draft unchanged after the picker closes (test:
  `apps/tui/src/slash/localCommands.test.ts::opens the picker without touching the composer, before and after it closes`)
- Deliberate differences: the reference emits a `/model … --provider … --reasoning …` command line;
  ours calls `config.set` (model, then reasoning) directly. The effort step starts on "keep the
  current effort" and is always offered (our provider rows carry no capability map). `q` filters.
- Not taken: provider warnings and model counts (not in our `ProviderOption`), provider display-name
  disambiguation, the fuzzy ranker (R3 owns `composer/fuzzy.ts`; plain term matching here).

## Status

Done (2026-10-19). Bridge follow-ups listed above.

## Wave 46 (RS4: a custom endpoint's models in the picker)

Read: none from the reference (a product observation from the P25 parity pass,
`docs/notes/P25/parity-compare.md`); our own `providers/model_list.py`, `cli/model_picker.py`.

- Cause: the TUI's `rowOf` already read an optional `models` list on each provider row, but
  `ProviderOption` had no such field and `model.options` never filled one, so every provider
  showed "this provider lists no models".
- `ProviderOption.models: list[str]` (default empty; TS regenerated, optional). `model.options`
  fills it for the **current** provider only, and only when its profile is a custom or local
  endpoint (`profile.is_custom`, listing supported): the session's own route and HTTP client when a
  session is named, else the resolved route with a short-lived client. The list comes from
  `model_list.list_models` with the profile scope, so it shares setup's per-base-URL cache
  (`<home>/cache/model_lists.json`, `model_catalog.ttl_minutes`); any failure leaves the list empty
  and the picker keeps its "type a model id" row. `rowOf` reads the typed field.
- Tests: `tests/unit/tui_bridge/test_model_options_listing.py` (six listed ids reach the custom
  row, one GET; a 503 leaves it empty; a vendor provider is never asked). Fail-first: on main the
  rows have no `models` field. Vitest pickers suite unchanged and green.

### Decisions for the lead
- W46-f-1 (D-16.1008): only the current custom/local provider is listed live. Listing every local server
  (Ollama, LM Studio) or vendor catalogue on each `/model` would cost up to the 10 s list timeout
  per unreachable server; vendors keep the typed id (setup's curated aliases are not sent yet).
  Follow-up if wanted: send `profile.model_aliases` for vendor rows, and list a non-current
  custom provider once it is picked (needs a per-provider `model.options` call).
