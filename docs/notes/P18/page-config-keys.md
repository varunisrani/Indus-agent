# Task: Config and Keys pages (P18-20) Phase: 18 Owner: builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/ConfigPage.tsx` (schema
use, category grouping, nested keys, save/reset/export: skimmed by search), `web/src/pages/EnvPage.tsx`
(row layout: set/unset badge, redacted preview, signup link, advanced split), `EnvVarInfo` in
`web/src/lib/api.ts`. Research spec 09 §3.2 (Config, Keys). Evidence: none.

What it does (own words): Config renders one form from the schema endpoint: each dotted key becomes a
control (enum → select, boolean → switch, number → number input, anything else → text), grouped
under its category in the server's category order. Save sends the whole object at once; Reset only
throws away unsaved edits (no request); Export downloads the current form as JSON and Import loads a
JSON file into the form (still unsaved until Save). A second tab, "Effective", lists every key with
its value and a chip naming where the value came from (default, file, env, admin overlay). A note
says changes apply to the next session or daemon restart. Keys lists each secret with set/unset,
redacted preview, description, signup link, a password input, Save and Delete; advanced keys are
behind a toggle. After Save the input is cleared and only the redacted preview is shown again.

Inputs → outputs: `GET/PUT /api/config`, `GET /api/config/schema`, `GET /api/config/effective`,
`GET/PUT/DELETE /api/env` → form state, toasts.

Files: `apps/web/src/pages/{ConfigPage,EnvPage}.tsx`, `apps/web/src/pages/{ConfigPage,EnvPage}.test.tsx`,
`apps/web/src/lib/nested.ts`.

Edge cases / failure modes:
- A schema key with no value in the config → control shows empty / default.
- Import of non-JSON or non-object → error toast, form untouched.
- A number field with junk → kept as text and refused before Save.
- Key save failure → the typed value stays in the input (so the user can retry), never echoed.

Our design differences / decisions:
- Effective-config view is ours (16 §2.6 item 3); no raw-YAML editor mode (not in the row).
- Values never round-trip through the page after a key save (R04.15): the response carries only
  the redacted preview.

Tests we will write:
- Enum/bool/string/number schema renders select/switch/text/number; Save sends edited value; Reset
  does not call the API; Effective tab shows source chips; key input cleared after save and redacted
  preview shown; advanced keys hidden until the toggle; delete confirms.

## Decisions for the lead
- `GET /api/config/effective` → `{entries: [{key, value, source}]}` with source in
  `default | file | env | overlay` (P18-12 to match).

Compare checklist (filled after the rewrite; reopened `ConfigPage.tsx`, `EnvPage.tsx` by search):
- [x] enum/bool/number/text controls grouped by category order (test: `apps/web/src/pages/ConfigPage.test.tsx::renders the right control for each schema type, grouped by category`)
- [x] Reset touches only the form (test: `ConfigPage.test.tsx::resets the form without calling the API`)
- [x] value never shown after save (test: `apps/web/src/pages/EnvPage.test.tsx::clears the input after save and shows only the redacted preview`)
- Deliberate differences: Reset discards unsaved edits (the reference resets the active category to defaults); no raw-YAML mode; no "reveal value" route (R04.15).

## Status

Done (2026-10-20). `/api/config/effective` owed by P18-12.
