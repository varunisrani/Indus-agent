# Task: Web model picker lists vendor models; Models page MoA row and per-model usage (W60-L1) Phase: ui-parity Owner: builder w60l1

Status: Done (2026-10-25) for both items, only as far as our backend has the data. Uncommitted in
the w60l1 worktree for the lead.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): no reference source opened; the
third compare's gap list and screenshots were the input.
Evidence: reference screenshots of the third compare (never in the repo), web `model-picker` and
`models`, light and dark (`$S/compare3/deliver/gaps.md`, "Web models").

What it does (own words):
- Model lists (W60-L1-1). What our backend knows per vendor provider: the provider profiles carry
  no model list (only reasoning / fast model hints), the daemon's `model.options` lists models live
  only for a custom or local endpoint, and the price table (`providers/pricing_table.yaml`) lists
  the model ids it prices for three vendors (OpenAI, Anthropic, Google), keyed by the vendor's own
  host. `GET /api/model/options` (already auth-gated and read-only) now adds to each provider row
  `models` (that table's ids for the vendor whose own host the provider's base URL names, HTTPS on
  443, the same rule pricing uses; table order), `models_source` (`price_table`) and
  `models_checked` (the day that block was read). An aggregator, a vendor the table does not price,
  a provider pointed at another host (a proxy, `override: true`) and the local / custom endpoints
  get an empty list and nulls: nothing is borrowed. No request leaves the machine.
- The web "Switch model" dialog shows those ids for the picked vendor (after the model in use and
  any endpoint list), counts them on the provider row ("openai · 26 models") in place of the key
  state, and says where the list is from ("From our price table (checked …)"). Custom endpoints
  still ask the endpoint (W57-H1-5); the typed model id stays as the fallback for every provider.
- Models page (W60-L1-2): the Model settings card gains a "Mixture of agents" row summing up the
  `moa` section from the config (the preset /moa uses: the default one, or the only one; its
  turned-on reference models and its aggregator as `provider:model`; the preset name when there
  are several; "N presets · no default" or "no presets" otherwise). Its Configure opens the Config
  page on the `moa` section: the Config page now honours `?section=<name>`. The web has no preset
  editor of its own (the desktop and `indus moa configure` have one).
- A card per model for the chosen period from `by_model` of `/api/analytics/usage` (W58-J1-1): rank,
  model id, a "main" tag on the main model, API calls; tokens in / out and cost only when
  `dashboard.show_token_analytics` is on (the totals card's rule). No usage: "No model calls in
  this period." The figures card above keeps the loading and error states of the same request.

Inputs → outputs: options rows → picker rows and model list; config `moa` → one summary line;
`by_model` → cards.

Files: src/indus/web/routes/models.py; tests/contract/web/test_profiles_models_routes.py;
apps/web/src/ui/ModelPickerDialog.tsx, pages/{ModelsPage,ConfigPage}.tsx (+ tests),
lib/restTypes.ts, testing/mockApi.ts, e2e/dashboard.spec.ts.

Edge cases / failure modes:
- A price table that fails to load gives every row an empty list (the picker falls back to the
  typed id), never an error.
- A model id typed in by hand for a vendor is still accepted; the list is a help, not a gate.
- `by_model` missing (an older backend) reads as no rows; a gateway that reports cumulative
  totals writes no per-model rows (W58-J1-1), so its sessions show in the totals only.
- A malformed preset (not a mapping, a slot without a model) is skipped in the count, as the
  tolerant reader does; disabled references are not counted.

## Decisions for the lead
- W60-L1-1: the list rides on the existing `/api/model/options` rows (new fields only) instead of
  a new route: one request, no route-list change, desktop unaffected. Only three vendors have a
  list (the price table's); the rest keep the typed id. No live `/models` call for vendors with a
  stored key (that would send the key out from a page view).
- W60-L1-2: MoA Configure is a link to Config › Moa rather than a web editor; the per-model card
  has no "Use as" (by_model names no provider, and setting a model without its provider would be a
  guess) and no "last used" (the store's per-model rows carry no time).

## Status
- [x] Price-table lists on options rows, aggregator / custom / proxy without (tests:
  `tests/contract/web/test_profiles_models_routes.py::test_options_list_the_price_table_s_models_per_vendor`)
- [x] Picker lists and counts them, hint, switch (tests: `ModelsPage.test.tsx` "lists a vendor
  provider's models from the price table"; e2e `dashboard.spec.ts::Models`)
- [x] MoA row and Config deep link (tests: `ModelsPage.test.tsx`, `moaSummary`,
  `ConfigPage.test.tsx` "opens on the section ?section= names")
- [x] Per-model cards, flag, empty state (tests: `ModelsPage.test.tsx`; e2e `dashboard.spec.ts::Models`)

## Compare checklist
- [x] model-picker: vendor rows show model counts; the right pane is a list with the typed id below
- [x] models: Mixture of agents row; per-model cards (rank, id, main tag, calls)
- [ ] Not matched on purpose: provider badge, "Use as" and "last used" on the model card (no data)
