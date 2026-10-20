# Task: Logs, Analytics and Files pages (P18-21) Phase: 18 Owner: builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/LogsPage.tsx` (filter
lists, refresh timer), `web/src/pages/AnalyticsPage.tsx` (response shape only, via `api.ts`),
`web/src/pages/FilesPage.tsx` (upload, download, delete confirm: skimmed by search),
`web/src/lib/api-files.ts`. Research spec 09 §3.2 (Logs, Files). Evidence: none.

What it does (own words): Logs tails one of three files (agent, errors, gateway) with a minimum
severity (all, DEBUG, INFO, WARNING, ERROR), a component (all, agent, tools, gateway, cli, cron) and
a tail length (50/100/200/500); it refetches every 5 s but only while the browser tab is visible and
colours each row by its level. Analytics shows input/output tokens and cost per day for 7, 30 or 90
days as simple bar charts drawn with SVG in our tokens, plus totals. Files browses the host: list a
folder, go up, make a folder, upload files, preview a text file, delete (after a confirm) and download
through a link that carries the session token (the one `?token=` REST route; it opens in a new tab).

Inputs → outputs: `GET /api/logs`, `GET /api/analytics/usage?days=`, `GET /api/files`,
`GET /api/files/read`, `POST /api/files/mkdir`, `POST /api/files/upload`, `DELETE /api/files`,
download URL `/api/files/download?path=…&token=…`.

Files: `apps/web/src/pages/{LogsPage,AnalyticsPage,FilesPage}.tsx`,
`apps/web/src/pages/{LogsPage,FilesPage}.test.tsx`, `apps/web/src/lib/visibility.ts`.

Edge cases / failure modes:
- Tab hidden → no polling; becoming visible → refetch at once, then every 5 s.
- Gated mode: no session token → the download link has no token (the cookie authenticates).
- Root locked by the server → no "up" past the root (`parent` null).
- Empty analytics → "no usage yet", no chart.

Our design differences / decisions:
- Charts are hand-drawn SVG (no chart library is named by the row; keeps the bundle small).
- No polling while hidden (the reference keeps polling; the row asks us not to).

Tests we will write:
- Fake timers: refresh at 5 s intervals, none while `document.hidden`; filters go into the query;
  rows coloured by level; download href carries the token; delete asks to confirm and only then calls.

## Decisions for the lead
- Upload is multipart `POST /api/files/upload` with fields `path` and `file` (P18-16 to match).

Compare checklist (filled after the rewrite; reopened `LogsPage.tsx`, `FilesPage.tsx` by search):
- [x] filter lists and 5 s refresh, paused while hidden (test: `apps/web/src/pages/LogsPage.test.tsx::refreshes every 5 s, and not while the tab is hidden`)
- [x] `?file=` deep link (test: `LogsPage.test.tsx::opens the file named in the URL`)
- [x] download link carries the token in loopback only (tests: `apps/web/src/pages/FilesPage.test.tsx::puts the session token on the download link`, `::omits the token in gated mode (the cookie authenticates)`)
- [x] delete asks first (test: `FilesPage.test.tsx::asks before deleting`)
- Deliberate differences: download is a token link opened in a new tab (row), not a data-URL fetch; SVG bars instead of a chart library; no drag-and-drop upload.

## Status

Done (2026-10-20).
