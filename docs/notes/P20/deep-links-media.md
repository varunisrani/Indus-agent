# Task: Deep links and media scheme (P20-34) Phase: 20 Owner: wave-35 builder D3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/deep-link-protocol.ts`
(all), `apps/desktop/electron/media-protocol.ts` (all), `apps/desktop/electron/media-range.ts` (all),
`apps/desktop/electron/main.ts` lines 17,953–18,300 (link parsing, the close-preview path, the
pending link, the ready signal, second-instance and open-url, cold-start argv), and
`main-window-lifecycle.ts` `activateWindow` (how a link raises a hidden window). Closed before
writing.
Evidence: none.

What it does (own words): `indus://<kind>/<name>?a=b` (dev builds `indus-dev://`) is parsed in main
into `{kind, name, params}`. Kinds: `open` (go to an app path inside the window — validated by the
renderer's route table, never a URL), `skill` (install a skill from the hub after the person confirms
in a dialog that names it), `mcp` (add an MCP server after the person confirms; from a link only an
`https` URL server, never a command), `close-preview` (handled entirely in main: restore and show
the main window, focus it, leave full screen, then tell the renderer to close the preview pane — it
works even when the page cannot hear anything). Every other kind, a malformed link or one longer
than 4 KiB is dropped and logged. Links reach main three ways — the cold-start argv, a second launch
(`second-instance` argv) and macOS `open-url` — and all go into one queue. Until the main window's
renderer says it listens (`indus:deep-link:ready`) only the latest link is kept; on the signal it is
delivered exactly once. A renderer reload or crash makes main wait for a new signal. Delivery
un-hides a tray-hidden or minimised main window without taking focus from another app (show
inactive; no focus call) — R08.21.

`indus-media://stream/<percent-encoded path>[?profile=x]` serves media to the app's pages. Local
connection: the file is read from disk only when its real path (symlinks resolved) is inside an
allowed root (home, the session workspaces main knows, the Indus home's media/cache folders), is
not a secret file (the shared guard), and has a media type we serve (images except SVG, audio,
video). Answers honour `Range` (`bytes=a-b`, `a-`, `-n`): 206 with `Content-Range`, 416 when
unsatisfiable, 200 without a range, HEAD answers headers only; multi-range requests get the whole
file. Remote connection: main mints a single-use download ticket with its own credential
(`POST /api/files/download-ticket`) and streams `GET /api/files/download?path&ticket` from the
remote, forwarding only range and cache headers; the renderer never sees the token or the ticket.
Methods other than GET/HEAD → 405; bad path → 404; outside roots or secret → 403; unknown type → 415.

Inputs → outputs: argv / second-instance / open-url → queue → `indus:deep-link {kind, name,
params}` to the main window (or `indus:preview:close-requested` for close-preview);
`indus-media://` request → Response (200/206/403/404/405/415/416/502).

Files: `apps/desktop/electron/deepLinks.ts`, `apps/desktop/electron/mediaProtocol.ts`,
`apps/desktop/electron/deepLinks.test.ts`, `apps/desktop/electron/mediaProtocol.test.ts`, renderer
`apps/desktop/src/deepLinks/{DeepLinkHost.tsx,deepLinkActions.ts,mediaUrl.ts,strings.en.ts}`
(+ `deepLinkActions.test.ts`, `DeepLinkHost.test.tsx`). Shared: `electron/main.ts` (queue fed pre-ready, install lines,
media handler), `src/app/App.tsx` (layer slot), `src/main.tsx`.

Edge cases / failure modes:
- two links before ready → only the latest is delivered, once; a third after ready → delivered at
  once; ready twice from the same page → no second delivery;
- link while no main window exists → the window is opened, the link waits for its ready;
- `indus://open/https://evil` or `indus://open/../x` → the renderer's classifier says unknown → ignored;
- `indus://mcp/x?command=sh` → refused (links only add URL servers), confirmation never shown;
- skill and mcp names longer than the backend allows → refused before the dialog;
- media: `%2e%2e` traversal, symlink out of a root, `id_ed25519`, `.env`, a directory → 403/404;
  `Range: bytes=10-19` → 206 with 10 bytes; `bytes=999999-` past the end → 416; remote 401/403 kept,
  other remote failures → 502.

Our design differences / decisions:
- One URL host (`stream`) for both connection kinds; main picks local disk or the remote proxy from
  the current connection, so a page never decides where a path lives (R08.23).
- Images (not SVG) are served too, not only audio/video, because the CSP's `img-src` names
  `indus-media:` for local images in the transcript.
- No `--close-preview` argv flag in 1.0 (the link covers it).

Tests we will write:
- parse: kinds, names decoded, params, wrong scheme, too long, malformed;
- queue: two links before ready → latest once; ready flush exactly once; reload resets readiness;
  close-preview handled in main (restore, show, focus, leave full screen, event) even before ready;
  delivery shows a hidden window inactive and never calls focus;
- media: Range 206 with 10 bytes; 416; HEAD; outside roots 403; symlink escape 403; secret 403;
  wrong type 415; method 405; remote proxy mints a ticket with main's token, forwards Range, never
  exposes the token in the answer;
- renderer actions: `open` validated by the route table; skill/mcp confirm before the REST call,
  cancel → no call; mcp command link refused.

## Decisions for the lead
- Links may add only `https` MCP servers (`indus://mcp/<name>?url=https://…`); a command (stdio)
  server from a link is refused — a link must never be able to start a program, even after a click.
- Skill links install with `force: false` only; a `caution` verdict shows the backend's refusal.
- Delivery shows the hidden window inactive and does not focus it (R08.21); close-preview focuses
  (the person asked to get out of a full-screen preview).
- Media allowed roots: the home directory, the Indus home's `media/` and `cache/` folders, and
  any workspace roots main is told about (P20-29's fs IPC can add its session workspaces through
  `MediaRoots.add`).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Parsing and kinds; wrong scheme, unknown kind, credentials, oversized → dropped (test:
  `electron/deepLinks.test.ts::parseDeepLink …`)
- [x] Latest-only queue, flushed exactly once on ready; reload/crash resets readiness; cold-start
  argv, second-instance and open-url feed one router (tests: `deepLinks.test.ts::two links before
  ready …`, `::a reload or crash waits for a fresh ready signal`; wiring in `electron/main.ts`)
- [x] close-preview entirely in main, also before the page listens (test: `::close-preview is handled
  in main …`)
- [x] Delivery un-hides without focus (test: `::delivery shows a tray-hidden window inactive …`)
- [x] Range responses 206/416/200/HEAD; roots, symlink escape, secrets, types, methods (tests:
  `electron/mediaProtocol.test.ts`)
- [x] Remote proxy with main-side auth; only range/cache headers forwarded (test:
  `mediaProtocol.test.ts::mints a ticket with main's credential …`)
- [x] Renderer: listen before ready; skill/mcp confirm; command links refused (tests:
  `src/deepLinks/DeepLinkHost.test.tsx`, `deepLinkActions.test.ts`)
- Deliberate differences: delivery shows the window inactive (the reference activates it); one
  `stream` host for both connection kinds (main picks disk or remote); images served as well as
  audio/video (not SVG); remote media uses a single-use download ticket per request instead of the
  token on the media request; links may add only https (or loopback http) MCP servers.
- Not taken: the `--close-preview` argv flag, the Windows hardware-key path, OAuth-cookie remote
  media (N17), WSL path bridging for media.

## Status

Done (2026-10-21). Shared: `electron/main.ts` (the router is created pre-ready and fed by
second-instance, open-url and the cold-start argv; `installMediaProtocol` + `installDeepLinks` install
lines; a second launch with a link no longer raises the window itself), `src/app/settingsSlots.tsx`
mounts `DeepLinkHost`. Hand-on: P20-28's preview pane listens on `preview.onCloseRequested`; P20-29 can
add session workspaces to the media roots (`MediaRoots.add`, today home + `<home>/media` + `<home>/cache`).
