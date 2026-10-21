# Task: cloud browser providers, user CDP endpoint, real-profile mode (P24-38) Phase: 24 Owner: wave-38 builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/browser_tool_cloud.py`
(provider resolution, explicit vs auto-detect, the private-URL switch, the real-profile consent
switch read on every call), `tools/browser_tool_real_profile.py` (profile snapshot, launch on the
copy, attach, the 30 s preparation lock, refusal texts), `agent/browser_provider.py` (the session
metadata contract), `plugins/browser/_common.py` (create/release lifecycle, close status codes),
`plugins/browser/browser_use/provider.py`, `plugins/browser/browserbase/provider.py`,
`plugins/browser/firecrawl/provider.py` (endpoints, headers, bodies, the 402 retry ladder);
research 02 §3.17, §3.25. Closed before writing. Evidence: none.

Vendor interfaces pinned (interop facts, checked against each vendor's public API docs at the
versions the paths name): Browser Use cloud API **v3** — `POST https://api.browser-use.com/api/v3/browsers`
with header `X-Browser-Use-API-Key`, answer `{id, cdpUrl, timeoutAt}`; release
`PATCH /browsers/{id}` body `{"action": "stop"}`. Browserbase API **v1** — `POST
https://api.browserbase.com/v1/sessions` with header `X-BB-API-Key`, body `{projectId, keepAlive?,
proxies?}`, answer `{id, connectUrl}`; release `POST /v1/sessions/{id}` body `{projectId, status:
"REQUEST_RELEASE"}`; a 402 means a paid feature is missing. Firecrawl API **v2** — `POST
https://api.firecrawl.dev/v2/browser` with `Authorization: Bearer`, body `{ttl}`, answer `{id,
cdpUrl}`; release `DELETE /v2/browser/{id}`. A release answered 200, 201 or 204 counts as closed.

What it does (own words): besides the local Chromium of P10, a profile can choose a cloud browser
(`browser.provider`: `browser-use`, `browserbase`, `firecrawl`; default `local`). A cloud session
is created through the vendor's REST API with the user's key read from the profile's
`SecretStore` (then the scope's env), and the session's CDP URL is attached to like a user CDP
endpoint, with the P10 request guard on our tab. Every cloud session is released again on every
way out: a normal close, the janitor's idle close, a force reap, process shutdown, and failures
half way (attach or guard install failed). A provider failure (an error answer, a dropped
connection, an attach that fails) falls back to local Chromium once for that task's session, and
the next tool result carries a notice saying so; the session keeps the fallback until it closes,
so the paid API is not hammered on every call. The user CDP endpoint (`/browser connect`,
`INDUS_BROWSER_CDP_URL`, `browser.cdp_url`) stays attach-only: a failed attach is an error, never
a launch. Real-profile mode (`browser.real_profile: true`, opt-in) launches Chromium directly on
the user's own browser profile folder (`browser.real_profile_dir`, else the platform's default
Chromium folder) under the dedicated session name `real-profile`; only one task may hold it, and a
task that finds it held waits up to 30 s for the holder to let go, then gets a clear refusal.
Nothing is ever copied out of the profile, and closing the session never deletes the folder.

Inputs → outputs: `browser.provider`, `browser.real_profile`, `browser.real_profile_dir`, the
vendor secrets (`BROWSER_USE_API_KEY`; `BROWSERBASE_API_KEY` + `BROWSERBASE_PROJECT_ID`, optional
`BROWSERBASE_BASE_URL`; `FIRECRAWL_API_KEY`, optional `FIRECRAWL_API_URL`) → a `BrowserSession`
whose `engine` label says what served it (`cloud:<provider>`, `local`, `cdp`, `real-profile`) and an
optional one-time `notice` shown in the next navigate result.

Files: `src/indus/tools/builtin/browser/cloud.py`, `src/indus/tools/builtin/browser/real_profile.py`,
tests `tests/unit/tools/builtin/browser/test_cloud.py`, `test_real_profile.py`; shared:
`browser/session.py` (choice modes, cleanups, fallback), `browser/tools.py` (engine + notice in
navigate results), `foundation/config/schema.py` rows `browser.provider`, `browser.real_profile`,
`browser.real_profile_dir`.

Edge cases / failure modes:
- missing key → a clear "set BROWSERBASE_API_KEY …" error, and the fallback to local (the key names
  are shown, never values); an answer body echoed in an error is redacted and cut to 200 chars;
- Browserbase 402: drop `keepAlive`, retry; still 402: drop `proxies`, retry;
- create succeeded but attach/guard failed → the session is released before the fallback;
- release fails (5xx, network) → logged with status only, never raises, never blocks shutdown past
  its 10 s timeout;
- a second task asks for the real profile while the first holds it → waits on the fake clock;
  30 s → refusal naming the holder's session name; the holder closing earlier hands it over;
- real profile folder missing, or held by a running browser (`SingletonLock` present) → refusal
  telling the user to close that browser; we never remove the lock file;
- consent off → real-profile mode is never chosen, whatever the folder setting says.

Our design differences / decisions:
- The reference snapshots the profile into a copy before launching; we never copy cookies or
  logins anywhere (owner rule): Chromium runs on the user's own folder, so the user's browser must
  be closed while the agent uses it.
- Providers are small clients over `httpx` in one module (no plugin registry); no managed-gateway
  billing path; no auto-detection from present keys — the profile names its provider.
- The one-time fallback notice and the `engine` label ride in the navigate result.

Tests we will write:
- per provider with `respx`: create → attach → close pairs exactly one release; release on the
  attach failure path, the guard failure path, force reap and shutdown;
- provider 5xx → one fallback to local with the notice once; later calls stay local, no second
  create; Browserbase 402 ladder; credentials never in errors or logs;
- user CDP endpoint never launches on attach failure;
- real profile: launch on the user's folder (not copied, not removed at close), lock refusal after
  30 s of fake time, hand-over when the holder closes, refusal when the folder is held, consent off.

## Decisions for the lead
- Real-profile failures are refusals (the reference falls back to a fresh browser with a note); a
  silent fresh browser would make the agent act signed-out on the user's behalf.
- Chrome ≥ 136 refuses remote debugging on its *default* data folder; users of Google Chrome
  stable must point `browser.real_profile_dir` at a non-default profile folder they signed into.
  Documented in the refusal text when the launch fails.
- Extra `browser-cloud = ["playwright==1.63.0"]` (cloud sessions attach through Playwright; the REST
  clients use `httpx`, already a base dependency). No new package.

Compare checklist (filled after the rewrite; reopened the files above):
- [x] create/close lifecycle per provider and close status set {200, 201, 204} (test: `test_cloud.py::test_each_provider_creates_and_releases_once`)
- [x] Browserbase 402 drops keepAlive then proxies (test: `test_cloud.py::test_browserbase_402_drops_paid_features_in_order`)
- [x] Browser Use answer may carry `connectUrl` instead of `cdpUrl` — handled, pinned by `test_cloud.py::test_browser_use_accepts_connect_url`
- [x] the 30 s real-profile preparation lock (test: `test_real_profile.py::test_held_profile_is_refused_after_thirty_seconds`)
- [x] consent switch read on every call (test: `test_real_profile.py::test_consent_off_never_uses_the_profile`)
- Deliberate differences: no profile snapshot/copy; no managed gateway; no auto-detect; no
  idempotency keys (direct mode only, as the reference's direct path).
- Not taken: Browserbase stealth/timeout knobs from env, provider setup wizard rows, headed mode for
  the real profile (it follows `LaunchSpec.headless`).

## Status

Done (2026-10-21). Wiring for the lead in the report (extra, toolset, docs).
