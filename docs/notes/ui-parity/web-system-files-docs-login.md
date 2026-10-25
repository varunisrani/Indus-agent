# Task: Web System, Console, Files, Docs, Login, error state (W52-f) Phase: ui-parity Owner: builder w52f

## Status

Done (2026-10-24), pending the lead's merge. All slugs of this slice are done: system, system-console, files,
docs, docs-direct-url, login, error-state (System/Files wiring; Skills adoption belongs to slice c); language-menu,
theme-menu and sidebar-collapsed needed no page work. Nothing remains in this slice except the follow-ups below. Rewrite and Compare complete; side-by-side PDF reviewed page by page
(`$SP/w52f/compare.pdf`). Shared files edited (additive): `shell/routes.ts` (the `/docs` row), `shell/Sidebar.tsx`
(`/docs` icon, the external docs link removed), `plugins/slots.tsx` (`docs` page slots), `ui/controls.tsx`
(`QueryState.what`, `ErrorState` measured look), `ui/ActionLog.tsx` (restyle, same props), `lib/format.ts` (two new
helpers).

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none by this builder. The lead's spec
(`w52-specs/web-system-files-docs-login.md`, Read + Note done by the lead from web `pages/SystemPage.tsx`,
`pages/FilesPage.tsx`, `pages/DocsPage.tsx`, `components/HermesConsoleModal.tsx`,
`components/LoadErrorNotice.tsx`, `lib/load-error-copy.ts`, `hermes_cli/dashboard_auth/login_page.py`) is the
starting point; this builder worked from it and from the screenshots only.
Evidence: owner's screenshot set of the running reference web dashboard, slugs `system`, `system-console`, `files`,
`docs`, `docs-direct-url`, `login`, `error-state`, `language-menu`, `theme-menu`, `sidebar-collapsed` (light and
dark). None of them are in the repo.

What it does (own words): the System page becomes one column of headed sections (Host with the update footer,
Daemon, Memory, Credential pool, Operations, Checkpoints, Shell hooks) with one shared action-log card on top;
the admin console becomes a large modal with a real terminal (xterm) where the prompt line is the input; Files gets
a path badge in the header, a path row with GO / UPLOAD / CREATE, a dashed drop zone and a grid table; `/docs`
becomes an in-app page with a framed panel; the sign-in page gets a fixed dark amber look; a failed page load shows
a red notice with Retry instead of a vanishing message.

Inputs → outputs: our existing REST calls (`api.system.*`, `api.daemon.*`, `api.files.*`, `/api/console`) →
the same calls; only markup, layout and styling change. Login: providers → server-rendered HTML under the
same CSP (style hash, no script, no fonts).

Files: `apps/web/src/pages/SystemPage.tsx`, `apps/web/src/pages/system/*.tsx`, `apps/web/src/ui/ActionLog.tsx`,
`apps/web/src/console/ConsoleModal.tsx` (+ `consoleTerminal.ts`), `apps/web/src/pages/FilesPage.tsx`,
`apps/web/src/pages/DocsPage.tsx`, `apps/web/src/shell/routes.ts`, `apps/web/src/shell/Sidebar.tsx`,
`apps/web/src/lib/format.ts`, `apps/web/src/ui/controls.tsx` (QueryState `what`), `src/indus/web/auth/login_page.py`,
tests next to each.

Edge cases / failure modes:
- each System section fails on its own → that section shows the load-error notice with Retry;
- host numbers we lack (OS, arch, host name, Python, uptime) → tiles left out, never shown empty;
- console socket refused / closed → the close reason printed in red, status badge "closed", Reconnect button;
- console confirm step → "Confirm? [y/N]" prompt in the terminal; anything but y/yes cancels;
- files: empty path → error toast; a path outside the locked root → the server's refusal in the table's error strip;
  folder names with "/" or "." / ".." refused before the request;
- drag of non-files over the drop zone → ignored;
- docs: a reload of `/docs` must render the SPA page (our FastAPI has no API explorer at `/docs`).

Our design differences / decisions: see the decisions below.

Tests we will write: System sections (host tiles, update footer + confirm, daemon stop confirm, doctor tail in the
page log, backup download, restore confirm, pool add/remove, memory, hooks modal, checkpoints prune confirm, a
section's load error with Retry); Console with a fake terminal (input frame, confirm y/N, cancel, Ctrl+C, close
reason, prompt); Files (parent row, create dialog, drop zone upload, absolute dates, delete confirm); Docs page
(heading, OPEN DOCS link); Playwright direct load of `/docs`; login page markup (pytest).

## Decisions for the lead

- W52-docs-1: `/docs` is an in-app page with the framed panel; until a docs site exists the panel holds a
  placeholder with an OPEN DOCS link to `DOCS_LINK.href` (new tab). Switching to an iframe later needs a docs
  domain and a `frame-src` (follow-up F3).
- W52-lang-1: the language menu lists English only (foundation already does this; nothing added here).
- W52-sys-1: sections and buttons without an API are omitted (provider-portal card, skill curator, security audit,
  update skills, prompt size, support dump, migrate config, debug share, memory file resets).
- W52-sys-2: our checkpoint rule inputs (keep days, size cap) and the restore "Overwrite existing files" checkbox stay.
- W52-files-1: a file click keeps opening our text preview (the reference downloads).
- W52-login-1: fixed dark amber sign-in page in both themes, system fonts only, the backdrop covers the whole viewport.
- W52-f-1: the console keeps its sidebar entry ("Admin console", foundation's footer) and also opens from
  System › Operations › "Open console".
- W52-f-2: `QueryState` gains an optional `what` (the thing that failed to load), so its Retry notice reads
  "Could not load <what>…" with the error under "Details:"; pages that do not pass it are unchanged.

## Compare checklist (spec §11; L = light, D = dark; re-captured with `$SP/w52f/kit/capture-f.ts`)

System
- [x] L [x] D single column, sections 32 px apart, heading icon + tracked heading, square bordered unfilled cards
  (test: `SystemPage.test.tsx` uses the section headings Host / Daemon / Memory / Credential pool / Checkpoints / Shell hooks)
- [x] L [x] D Host: 3-col tiles, version badge ("latest" / "N behind"), divider + "Check for updates" footer with message
  (`shows host numbers in tiles…`, `badges commits behind and asks before updating`)
- [x] L [x] D Daemon row: badge + description left, START / RESTART / STOP (warning) right; stop confirm still works
  (`asks before stopping the daemon, then tails the stop`)
- [x] L [x] D Memory: provider readout row + divider + built-in sizes; select/Save/Reset work (`selects a memory provider…`)
- [x] L [x] D Pool: 1:2:1 form, right ADD KEY, bordered entry rows with badges + trash (`adds a pool key…`)
- [x] L [x] D Operations: icon ghost buttons; backup/restore card with dividers; restore confirm
  (`offers the backup for download…`, `restores by path after confirming…`)
- [x] L [x] D Checkpoints summary + Prune (confirm); Hooks header with NEW HOOK, hook cards, modal
  (`lists checkpoints and prunes…`, `adds a shell hook only with consent…`)
- [x] L [x] D action log card on top with running/done/exit badge and close (`tails the doctor log…`)
- [x] per-section load error with Retry (`shows one section's load failure with a Retry that refetches`)
Console
- [x] L [x] D 960 px panel, 72 px header with icon box, INDUS CONSOLE, status badge + profile, close X
- [x] L [x] D xterm body (black on dark; light canvas in a black frame on light), prompt "indus>", help output does not wrap
  (`ConsoleModal.test.tsx`: input frame, y/N confirm, cancel, Ctrl+C, history, close reason + Reconnect)
Files
- [x] L [x] D header path badge + refresh icon
- [x] L [x] D path input + GO, UPLOAD, CREATE outlined buttons
- [x] L [x] D dashed drop zone with icon box, DROP FILES HERE, path, CHOOSE FILES; drag state (`uploads what is dropped…`)
- [x] L [x] D table: head 31 px uppercase, ".." row, 44 px rows, amber folder icons, open/download + red trash icons,
  absolute dates (`shows sizes and absolute dates…`, `opens folders, goes up and previews text`)
- [x] L [x] D create-folder dialog; preview dialog; delete confirm (`makes a folder…`, `asks before deleting`)
Docs
- [x] L [x] D sidebar "Documentation" is an in-app item; white framed panel with placeholder + OPEN DOCS
  (`App.test.tsx` "lists Documentation as an in-app page…", `DocsPage.test.tsx`)
- [x] direct load / reload of /docs shows the same page (Playwright `dashboard.spec.ts` "Docs: a direct load…")
Error state
- [x] L [x] D red-bordered notice, icon, red line, muted details, outlined Retry; Retry refetches — wired on System
  (per section) and Files (table strip). The `/skills` 500 capture still shows the Skills page's own notice until
  slice c passes `what`/`onRetry` (see follow-ups); `error-state-system` shows this slice's notice.
Login
- [x] fixed dark amber page in both themes: wordmark, 416 px card, SIGN IN, subtitle, amber buttons, form, footer rules
- [x] wrong password error shows; CSP unchanged (`default-src 'none'`), no script, no font files
  (`tests/unit/web/auth/test_login_page.py`, Playwright `auth.spec.ts`)
Language / theme / rail
- [x] EN menu (English only, checked) — foundation's
- [x] theme popover above all page layers; collapsed rail: pages fluid (Host grid stays 3 columns)

- Deliberate differences: Host shows only the tiles we have data for (no OS / Arch / Host / Python / Uptime, F1);
  no provider-portal, skill-curator, security-audit, update-skills, prompt-size, support-dump, migrate-config or
  debug-share controls and no memory-file resets (no APIs, W52-sys-1); our checkpoint rule inputs and the restore
  overwrite checkbox stay (W52-sys-2); a file click opens our text preview (W52-files-1); `/docs` holds a pointer
  to the docs instead of an embedded site, and its reload renders the page rather than an API explorer (W52-docs-1);
  the sign-in backdrop covers the whole viewport and the password block's title is the provider's display name
  (W52-login-1); the admin console also keeps its sidebar entry (W52-f-1); page content ends at x 1416 (the
  foundation's gutter) where the reference's ends at 1407.
- Not taken: the reference's console font and colour picker; the files `broken_link` state (no such field).

Follow-ups:
- F1 (backend): `os`, `arch`, `hostname`, `python`, `uptime_s` in `/api/system/stats`; the tiles slot in.
- F3 (backend): docs domain + a `frame-src` for it, then the docs panel becomes an iframe.
- Other page slices adopt `QueryState what=… onRetry=…` for their load errors (Skills for the `error-state` slug).

## Integration (wave 53)

W52-docs-1, -lang-1, -sys-1, -sys-2, -files-1 are D-16.1203, W52-login-1, -f-1, -f-2 are D-16.1204. `format.ts` keeps slice b's and this slice's helpers side by side; the 20 s timeout this slice put on the App docs test is removed (root cause in D-16.1209: page modules now load before the test runs).
