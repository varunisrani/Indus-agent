# Task: Files pane and fs IPC (P20-29) Phase: 20 Owner: wave-35 builder D2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/fs-ipc.ts` (all),
`apps/desktop/electron/fs-read-dir.ts` (all), `apps/desktop/electron/hardening.ts` (sensitive-file
guards, path syntax, the readable-file and directory resolvers, the data-URL reader: lines ~16–36 and
~285–605), `apps/shared/src/data-url-read-max.ts` (default and bounds of the Settings value), the
data-URL handlers in `apps/desktop/electron/main.ts` (~16,583–16,660). Closed before writing.
Evidence: none.

What it does (own words): the files pane shows the session's workspace as a tree. In local mode it
reads the disk through main's fs IPC; in remote mode it reads the remote host through the REST proxy
(`GET /api/files`, `GET /api/files/read`, `DELETE /api/files`, P18). Folders expand lazily; a row
offers preview (to the preview pane), rename, move to trash, reveal in the OS file manager and copy
path; watched folders refresh themselves; drag-drop of rows hands the path to whoever accepts it
(the composer) as `text/plain` and an `application/x-indus-path` item.

Main's fs IPC (every handler registered through the P20-03 registry, so sender-checked and
zod-validated) confines every path:
- the path must be absolute (no NUL, no Windows device path), is normalised, and must lie inside an
  allowed root: the user's home, or a workspace root the asking window declared with
  `fs.setRoots` (an existing folder that is not a filesystem root or a system folder), or a file the
  person picked in a native dialog from that window;
- the real path (symlinks resolved) must lie inside the real path of the same kind of root — a
  symlink inside the workspace that points at `/etc` is refused (no symlink escape);
- reads into the renderer refuse key stores and `.env`-like files (the shared `isSensitivePath`
  guard of P20-14), on both the requested and the real path;
- `readDataUrl` caps preview reads at the Settings value (default 16 MiB, 1–4,096 MiB, stored in
  userData) and attachment reads at 256 MiB; `stat` with purpose `attach` refuses more than 256 MiB;
- rename never overwrites and stays inside the roots; trash and reveal refuse the roots themselves;
- watchers belong to the window that asked (at most 64), debounce 120 ms, die with the window.

Native dialogs (`fs.selectPaths`): the picked paths are local paths of this computer; we never
translate them for a WSL or remote backend (there is no path bridging in 1.0) — in remote mode the
composer uploads the bytes (R08.23).

Inputs → outputs: `fs.setRoots {roots}` → accepted roots; `fs.readDir {path}` → entries (kind,
size, mtime, sensitive flag, max 5,000 + `truncated`); `fs.readText {path, maxBytes?}` → text,
truncated, binary, size; `fs.readDataUrl {path, purpose}` → data URL; `fs.stat {path, purpose}` →
size, kind, MIME; `fs.rename {from, to}`; `fs.trash`; `fs.reveal`; `fs.selectPaths`; `fs.watch` /
`fs.unwatch` + `fs.onChanged`; `fs.getLimits` / `fs.setLimits {dataUrlMaxMb}`.

Files: `apps/desktop/electron/fs/fsIpc.ts`, `apps/desktop/electron/fs/pathGuard.ts` (roots and
confinement, shared by preview, git and terminal), `apps/desktop/electron/fs/fsIpc.test.ts`,
`apps/desktop/electron/fs/pathGuard.test.ts`, `apps/desktop/src/panes/files/FilesPane.tsx`,
`apps/desktop/src/panes/files/source.ts` (local and remote sources), `apps/desktop/src/panes/files/FilesPane.test.tsx`.
Shared: channel table/schemas + `src/global.d.ts`, `electron/main.ts` (install line).

Edge cases / failure modes:
- `../../etc/passwd` (relative) and `<root>/../../etc/passwd` → refused; a symlink out of the root →
  refused; `id_ed25519`, `.env.local`, `.ssh/config` → read refused (listed with a lock);
- a 300 MiB file → attach refused; a preview read above the Settings cap → `too-large`;
- rename onto an existing name → refused, nothing overwritten; rename to another folder outside the
  roots → refused;
- a folder with 50,000 entries → first 5,000 and `truncated`;
- the window closes → its watchers and its declared roots are dropped;
- remote mode: rename/reveal are not offered; delete asks first and goes through REST.

Our design differences / decisions:
- Allowed roots are per window (`fs.setRoots`), not process-wide; dialog picks are granted to the
  window that showed the dialog.
- Sensitive files are listed (with a lock) but never read; the REST server leaves them out of its
  listing.
- `.env.example`-style templates stay refused (the shared guard is stricter than the reference).

Tests we will write:
- path guard: absolute only, traversal, symlink escape, roots validation (filesystem root and system
  folders refused), dialog grants, sensitive files;
- fs IPC through a registry with a fake ipcMain: read-dir sorting/caps, read-text truncation and
  binary, data-URL caps (preview from the Settings value, attach 256 MiB), stat 300 MiB refused,
  rename within the workspace works and never overwrites, trash/reveal, watch debounce and teardown,
  limits persisted;
- pane: lazy tree, remote source through `api.request`, rename/trash flows, sensitive rows not
  previewed.

## Decisions for the lead
- Four new channels: `indus:fs:set-roots`, `indus:fs:stat`, `indus:fs:limits:get`,
  `indus:fs:limits:set` (P20-32 Settings reads/writes the data-URL cap through the last two; the
  composer of P20-19 can replace its one-byte `fs.readText` path check with `fs.stat {purpose: "attach"}`).
- `fsEntry` gained `sensitive?`; `fs.readDir` answers `truncated`; `fs.readText` answers `binary`.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] `../../etc/passwd` refused; traversal and symlink escape refused (tests:
  `electron/fs/fsIpc.test.ts::../../etc/passwd and friends are refused`,
  `electron/fs/pathGuard.test.ts::refuses traversal out of a root`, `::refuses a symlink that escapes the root (and a root that is one)`).
- [x] `id_ed25519` read refused, by name and by real path (tests: `fsIpc.test.ts::id_ed25519 read refused (text, data URL, stat, preview watch); listed with a lock`,
  `pathGuard.test.ts::refuses sensitive files for reads, by name and by real path`).
- [x] 300 MiB attach refused; preview cap from the Settings value (default 16 MiB, 1–4,096) (tests:
  `fsIpc.test.ts::300 MiB attach refused (stat and data URL)`, `::data URLs: preview capped by the Settings value, attach at 256 MiB`, `::limits are clamped and persisted`).
- [x] Rename within the workspace works, never overwrites (tests: `fsIpc.test.ts::rename within the workspace works and never overwrites`,
  `src/panes/files/FilesPane.test.tsx::rename within the workspace goes to main with the new path in the same folder`).
- [x] Listing: folders first then by name, stat in batches, capped (tests: `fsIpc.test.ts::lists folders first, by name, without noise`, `::stops at 5000 entries`).
- [x] Device paths and NUL refused (test: `pathGuard.test.ts::wants an absolute, plain path`).
- [x] Native dialogs answer local paths, never translated; picks granted to that window only (test: `fsIpc.test.ts::dialog picks are local paths granted to the asking window only`).
- [x] Watched folders debounced and owned (tests: `fsIpc.test.ts::watches a folder: debounced, owned, closed with the window`,
  `FilesPane.test.tsx::declares the workspace to main, lists it lazily and watches what is open`).
- [x] Remote mode through REST, delete asks first (test: `FilesPane.test.tsx::lists through the REST proxy; no rename, reveal or local preview; delete asks first`).
- [x] Gap found: the reference's trash and reveal take any path; ours are confined and refuse the roots (`fsIpc.test.ts::trash and reveal stay inside the roots and never take a root`).
- Deliberate differences: allowed roots per window (home + declared workspaces + dialog picks) instead of
  whatever the renderer names; rename takes a full target path (confined, no overwrite) instead of a
  base name; fewer always-hidden names (`.git`, `node_modules`, `__pycache__`, `.DS_Store`,
  `Thumbs.db` — build folders stay visible); `.env.example`-style templates stay refused (stricter
  shared guard); paths must be absolute (no `~` expansion: the renderer always has absolute paths);
  a missing file is an `not-found` envelope (the registry never logs a rejected handler).
- Not taken: plugin roots/installs, logs root, `writeText`, `openDir` (not in the row); WSL path
  bridging (no WSL backend support in 1.0).

## Status

Done (2026-10-21). Shared: channel table/schemas + `src/global.d.ts` (four new channels, shapes
above), `electron/main.ts` (one `installPanes` step + `paneElectron`), `electron/panesInstall.ts`.
The composer (P20-19) can switch its one-byte `fs.readText` path check to `fs.stat {purpose: "attach"}`
and accept the `application/x-indus-path` drag (lead wiring).
