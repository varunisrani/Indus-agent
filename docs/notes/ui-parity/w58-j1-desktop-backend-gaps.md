# Task: Desktop gaps our backend can close (W58-J1) Phase: ui-parity Owner: builder w58j1

Status: Done for wave 58 (2026-10-25); items 2 and the "Move to project" half of 5 left out on
purpose (no backend behind them); see the end.

Read (reference): none opened. Evidence: the wave-55 desktop compare set
(`$S/compare2/{hermes,indus}/desktop/`), slugs `command-center-usage`, `tools`,
`settings-models-moa`, `settings-advanced`, `context-menu` (both themes), and
`$S/compare2/deliver/gaps.md` (top-10 items 3, 4 and 10). Our own code read first: the analytics
route, `state/usage.py`, the usage recording in `core/usage.py`, `foundation/config/schema.py`
(`moa`, `terminal`), `core/moa_presets.py`, `web/routes/config_env.py` + `config_view.py`,
`service/projects_rpc.py` + `state/projects.py`, the desktop window factory, navigation guard,
external opener and app menu.

What it does (own words):
1. **Command center › Usage** shows what the store already records. `GET /api/analytics/usage`
   gains `api_calls` (per day and in `totals`: the sessions' `api_call_count` plus side calls'
   `model_usage.api_call_count`) and `by_model` (the window's `model_usage` rows grouped by model,
   main loop and side calls, most tokens first). The desktop page shows the API-call count instead
   of "—, not counted yet" when the answer has it, and Top models lists up to five models with
   their tokens (a tooltip adds in/out, calls and cost). Per-skill usage per period is still not
   recorded, so Top skills keeps an honest empty line. The 0/0 in the wave-55 shots came from the
   capture's scripted model, which sent no usage; the real runtime records every answered call.
2. **Desktop & startup: Disable F12 DevTools / Always open links externally** — not added. DevTools
   exist only in dev builds (`webPreferences.devTools = dev && app window`; the menu item is
   dev-only) and nothing binds F12, so a packaged build has nothing to disable. Chat links always go
   through main's audited external opener; there is no in-app browser choice for a pref to switch.
   A row for either would be fake.
3. **Tools tab: Execution backend picker** already existed (wave 53, W52-k): the terminal toolset's
   detail has one card per `terminal.backend` value, writing through `PUT /api/config`. It was
   invisible because the tab opened on the first A–Z row (A2A). The tab now opens on the terminal
   toolset when nothing is picked, so the backend cards show at once.
4. **Model › Mixture of Agents "No presets yet"** — root cause: the capture's `config.yaml` has no
   `moa` section (we ship no default preset; the reference's screen shows one it ships). Checked
   end to end: a preset in `config.yaml` comes back from `GET /api/config` under `moa.presets`
   (probe through the REST test app), and the desktop page lists it (unit test, and a re-shot with
   a preset in the capture config). No code change; we do not invent a default preset.
5. **Session row menu: Open in terminal** opens the chat (its folder becomes the window's
   workspace, where the terminal pane's shells start) and opens/brings forward the terminal pane.
   Shown only for a row with a folder, when the pane may run (Advanced mode, local connection,
   desktop bridge with a terminal). **Move to project** is not added: `projects.*` has no method
   that assigns a session to a project (a session joins one only by its folder at creation).

Inputs → outputs: `GET /api/analytics/usage?days=` → `{days, daily[{day, input_tokens,
output_tokens, api_calls, cost, sessions}], totals{…, api_calls}, by_model[{model, input_tokens,
output_tokens, api_calls, cost}]}` → stat tiles and Top models. Row menu → `navigate(session)` +
`layout.open("terminal")` / `activate`.

Files: src/indus/web/routes/analytics.py, tests/contract/web/test_logs_analytics_routes.py,
apps/desktop/src/app/command-center/{CommandCenter.tsx,CommandCenter.test.tsx,strings.en.ts,
commandCenter.css}, apps/desktop/src/app/capabilities/{ToolsetsTab.tsx,CapabilitiesPage.test.tsx},
apps/desktop/src/sidebar/{SessionRowMenu.tsx,Sidebar.tsx,Sidebar.test.tsx,strings.en.ts},
apps/desktop/src/app/Shell.tsx.

Edge cases / failure modes:
- An older backend without `api_calls` / `by_model`: the page keeps "—, not counted yet" and
  "Per-model usage is not recorded yet." (fields are optional in the answer type).
- A gateway that reports cumulative totals (`mode="absolute"`) writes no `model_usage` rows: its
  sessions count in `totals` but under no model, so Top models can sum to less than the tiles.
- A session without a folder has no "Open in terminal"; simple interface mode, a remote connection
  or no desktop bridge hides it too. Opening it while a terminal runs in another folder restarts
  that terminal's shell in the new folder (the pane's existing cwd rule).

Our design differences / decisions:
- W58-J1-1: usage API calls and the per-model split come only from what the store counts; the web
  Analytics page may read the same two fields later (web slice, not changed here).
- W58-J1-2: no F12 DevTools or "open links externally" rows — no behaviour behind them.
- W58-J1-3: the Tools tab opens on the terminal toolset (its backend cards) instead of the first row.
- W58-J1-4: MoA empty state is correct for a config without presets; no default preset shipped.
- W58-J1-5: "Open in terminal" in the row menu; "Move to project" waits for a session → project
  route.

## Compare checklist
- [x] command-center-usage: API CALLS shows a number; tokens from recorded usage; TOP MODELS lists
      the model with its token count.
- [x] tools: opens with the terminal toolset and its Execution backend cards.
- [x] settings-models-moa: a configured preset shows (re-shot with a preset in config).
- [x] context-menu: "Open in terminal" next to Branch / Export.

## Status
Done (wave 58). Left: Desktop & startup rows (W58-J1-2) and "Move to project" (W58-J1-5) for lack
of a backend; backend readiness badges on the backend cards ("Ready" / "Needs setup") — we have no
route that reports them.
