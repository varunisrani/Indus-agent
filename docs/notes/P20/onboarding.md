# Task: Provider onboarding (P20-33) Phase: 20 Owner: wave-35 builder D3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/components/onboarding/index.tsx`
(all: when the overlay shows, the picker, the API-key catalogue, "choose later", manual mode, exit),
`flow.tsx` (head: flow panel per state, device-code display), the flow union in
`apps/desktop/src/store/onboarding.ts` (state names only). Closed before writing.
Evidence: none.

What it does (own words): once the backend is usable (the window's connection is open), the app asks
the backend whether any provider is configured. If none is, a modal overlay blocks the chat until
the person configures one or picks "Choose later". The picker offers: providers that sign in with a
device code (the code is shown large with a Copy button and an "open the page" link that goes
through the audited external opener; the backend polls the provider and the overlay polls the
backend), an API-key form listing every key-based provider the runtime knows plus a generic "paste a
key" row (variable name + value), a local model runtime endpoint (a loopback URL, no key) and a
custom OpenAI-compatible endpoint (URL + optional key). Endpoint choices are tested through the
backend (the endpoint's `/models`) before anything is saved. After a sign-in, a saved key or a tested
endpoint, a model-confirm panel lists the provider's models (or takes a typed id) and saves the main
model; the overlay then closes.

State comes from the backend every time (`GET /api/model/options`: providers with `auth_type`,
`configured`, `device_login`); nothing about "configured" is cached in local storage. "Choose later"
hides the overlay for this window session only; it comes back when a turn fails with a provider
error (the transcript's error card names the layer `provider` or `auth`) or when the person opens it
from Settings › Providers. Keys are saved through the backend only (`model.save_key` over the
daemon, `PUT /api/env` for the generic row); the renderer keeps no key after the call returns.
Decisions branch on `auth_type`, `device_login` and slugs, never on display names. No free-tier
flow (D-16.12).

Inputs → outputs: connection open → `GET /api/model/options` → hidden (configured) or picker; picks
→ `POST /api/providers/login/start` + polling `GET /api/providers/login/{id}` / `model.save_key` /
`PUT /api/env` / `POST /api/model/test-endpoint` → `POST /api/model/set` → closed.

Files: `apps/desktop/src/onboarding/{Onboarding.tsx,flow.ts,requests.ts,onboarding.css,strings.en.ts}`,
`flow.test.ts`, `Onboarding.test.tsx`. Shared: `src/app/App.tsx` (layer slot), `src/main.tsx`,
`src/i18n/en.ts` (spread).

Flow states (pure reducer in `flow.ts`): `checking` → `hidden` (configured) | `picker`;
`picker` → `device-starting` → `device-polling` (code shown) → `confirm-model` | `error`;
`picker` → `key-form` → `saving-key` → `confirm-model` | `key-form` (error);
`picker` → `endpoint-form` (local | custom) → `testing-endpoint` → `confirm-model` | `endpoint-form`;
`confirm-model` → `saving-model` → `done`; any → `dismissed` on "Choose later" (first run only);
`dismissed` → `checking` on a provider error or an explicit open; `error` → `picker` (other
provider) or back to the failed provider.

Edge cases / failure modes:
- options call fails (backend still starting) → "checking" stays, retried with backoff (2 s → 15 s);
- device flow: start 400 `no_device_flow` → error with "use an API key"; status `denied`, `expired`,
  `failed`, `cancelled` each → error text from the backend; the poll stops when the overlay closes
  (and `DELETE` cancels the backend session);
- key save fails → the form keeps the text, shows the backend's message; success clears the field;
- endpoint test unreachable / 401 → message, nothing saved; zero models → typed model id allowed;
- a provider configured from another surface while the picker is up → next check closes it;
- Esc does not dismiss the first-run overlay (Settings-opened mode has a close button).

Our design differences / decisions:
- "Choose later" is not persisted anywhere (per the row: never latched in local storage); a
  provider error or an explicit open brings it back. A relaunch with no provider asks again.
- No curated vendor list in the renderer: the backend's catalogue drives every row.

Tests we will write:
- reducer table: every (state, event) pair the flow uses, and that unknown events leave the state;
- overlay: configured → nothing rendered; unconfigured → picker; "Choose later" dismisses and a
  provider error re-shows it; a key is saved through the backend (fake records `model.save_key`)
  and no storage key holds it; device code shown with Copy; endpoint test then model confirm.

## Decisions for the lead
- "Choose later" lasts for the window session (not persisted), re-shown on a provider/auth error
  card or from Settings › Providers.
- Keys for catalogue providers are saved with the daemon's `model.save_key {slug, api_key}`; the
  generic "paste a key" row uses `PUT /api/env {key, value}`; endpoints use `POST /api/model/set
  {scope: main, provider: "custom", model, base_url}` and the key (if any) as the custom provider's
  key through `model.save_key {slug: "custom"}`.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Shown only when unconfigured, after a usable backend; manual open from Settings; Esc never
  dismisses the first run (tests: `src/onboarding/Onboarding.test.tsx::stays out of the way …`,
  `::waits for a usable backend before asking`, `::Settings can open it even when configured …`)
- [x] Device code with copy and the audited opener; polling; cancel on leave (tests:
  `Onboarding.test.tsx::device sign-in shows the code with copy, polls, then confirms the model`,
  `::leaving a device sign-in cancels it on the backend`)
- [x] Key catalogue from the backend + a generic row; key through the backend, never in storage (test:
  `Onboarding.test.tsx::a key is saved through the backend, never in renderer storage, …`)
- [x] Local runtime / custom endpoint tested through the backend, then model confirm (test:
  `Onboarding.test.tsx::a local runtime endpoint is tested through the backend …`)
- [x] Choose later and re-show on a provider error only (test: `::choose later dismisses; it comes back
  only on a provider error`); flow table (`src/onboarding/flow.test.ts`)
- Deliberate differences: "choose later" is not persisted (the reference keeps a durable skip); no
  curated vendor list or "show all" toggle stored in local storage — the backend catalogue drives the
  rows; decisions use `auth_type` / `device_login` / slugs only.
- Not taken: the reference's PKCE paste-code and external-CLI sign-in flows (only documented device
  flows, D-16.11), the free-tier intro (D-16.12), the "preparing" progress view (the boot overlay covers
  it), the launch-reason banner, local model downloads.

## Status

Done (2026-10-21). Mounted as a window layer by `src/app/settingsSlots.tsx` (ready = the primary
connection is open; events = its `onEvent`; keys through `model.save_key`). Settings › Providers opens
it with `requestOnboarding({reason: "manual"})`.

Integration (wave 35): a main model on a chosen custom endpoint (`model.provider: custom` with a model) counts as set up — a keyless endpoint would otherwise bring the overlay back on every launch (D-16.722, `flow.test.ts::a chosen custom endpoint (no key needed) → hidden`). Settings closes before it opens the flow (it rendered under the settings dialog's layer; D-16.723).
