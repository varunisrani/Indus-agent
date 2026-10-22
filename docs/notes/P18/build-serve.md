# Task: build and serve integration (P18-29) Phase: 18 Owner: builder P3 (wave 32)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/vite.config.ts` (dev token
plugin, `/api` proxy, chunk groups, output dir), `hermes_cli/main_dashboard.py`
(`_resolve_dashboard_web_dist` only: when the build runs, when a promised dist is checked),
`hermes_cli/main_web_build.py` (`_web_dist_dir`, `_build_web_ui` header: where the build lands, the
build lock), `setup.py` (docstring: what a wheel would lack). Research spec
`docs/plan/research/09-web-acp-mcp-platform.md` §3.1.
Evidence: none.

What it does (own words): the dashboard's pages are a Vite build of `apps/web`. Three places can
hold that build, and one small locator (`indus.web.bundle.locate`) decides which one the server
serves:

1. the runtime tree inside the package, `indus/web/static/` — what an installed tree has. A release
   build fills it by copying `apps/web/dist` there (`python -m indus.web.bundle publish`: staged
   next to the target, then swapped in, so a failed copy keeps the old pages); the wheel picks it
   up through a hatch `artifacts` entry (the directory is git-ignored), which is the packaging hook
   P23 consumes;
2. in a source checkout (the package sits under `<repo>/src/` beside `apps/web/package.json`),
   `apps/web/dist` — what `pnpm --filter @indus/web build` writes; it wins over a published copy
   there, because it is the developer's latest build;
3. nothing yet: the locator still answers the directory a build would fill (the source tree's
   `dist`, else the runtime tree), so the server's "not built" answer clears by itself once the
   files appear.

`indus dashboard` builds once before serving when nothing is found, only in a source checkout and
only with `pnpm` on PATH (heap and thread caps from P18-01). `indus serve` never serves pages. The
SPA catch-alls (`/api/...` JSON 404, assets, the index fallback) are mounted after every API and
WebSocket router, so a real route always wins.

Development: `pnpm --filter @indus/web dev` runs Vite on 127.0.0.1:5173 and forwards `/api` —
WebSocket upgrades included — to the backend (127.0.0.1:9119, or `INDUS_WEB_DEV_BACKEND`). Vite
serves its own `index.html`, which has none of the runtime globals the backend injects, so the
dev-only plugin (`apps/web/vite.dev-bootstrap.ts`) fetches the backend's `index.html` on every dev
page load, reads only the known `window.__INDUS_*__` assignments out of its bootstrap script,
re-encodes them and injects them into the dev page. It never logs a value and never runs in a
build. Builds split React (with the router), xterm and the rest of `node_modules` into their own
chunks (`apps/web/vite.chunks.ts`); a budget check after each build names any JavaScript chunk
over 600 kB (a CI build fails on it, elsewhere it warns).

Inputs → outputs: package dir + repo layout → `BundleLocation(directory, kind, found)`; backend
`index.html` → injected dev script; Rollup bundle → size report.

Files: `src/indus/web/bundle.py` (new), `apps/web/vite.dev-bootstrap.ts` (new),
`apps/web/vite.chunks.ts` (new: chunk naming moved out of the config, budget check),
`apps/web/vite.dev-bootstrap.test.ts` (new), `tests/unit/web/test_bundle_locate.py` (new); shared:
`apps/web/vite.config.ts` (imports the plugin, proxy, chunking and budget),
`apps/web/tsconfig.json` (includes the new files), `apps/web/src/test/setup.ts` (its browser reset
skips node-environment test files), `src/indus/web/app.py` (default bundle from the locator),
`src/indus/web/command.py` (first-launch check through the locator), `src/indus/web/spa.py`
(`default_bundle_dir` delegates), `pyproject.toml` (wheel `artifacts`), `.gitignore`
(`src/indus/web/static/`), `docs/internals/acceptance-map.md` (two ACC-L1-1 lines).

Edge cases / failure modes:
- runtime tree with a directory but no `index.html` (a half copy) → not found; dist used or build.
- source checkout with both → `dist`; installed tree never looks outside the package.
- publish into a missing parent, over an old copy, from a source without `index.html` (refused),
  and a crash between stage and swap (the old copy or the stage is left, never half of each;
  leftovers of an earlier run are cleared first).
- dev plugin: backend down, slow (3 s timeout), gated (a redirect, no token), returns HTML without
  our script, or a value that tries to close the script → nothing injected or re-escaped; a key we
  do not know is dropped.
- dev proxy: a WebSocket upgrade reaches the backend with its subprotocol header intact (the
  credential rides there, D-16.580).

Our design differences / decisions:
- Two candidate locations instead of one output dir inside the package: Vite keeps writing
  `apps/web/dist` (nothing generated lands in `src/` during development), the copy into the package
  happens only for a release.
- No environment override of the bundle directory (the reference has one for its desktop and Nix
  builds); our desktop uses the headless backend (D-16.89) and Nix builds the wheel with the files
  inside it.
- The dev plugin re-encodes parsed values instead of pasting the matched text.
- A budget plugin instead of relying on Rollup's warning alone; strict under `CI`. Measured on
  this tree: the largest chunk is `react` at 257 kB (vendor 76 kB, shell 23 kB).

Tests we will write:
- Python: installed bundle found; dev bundle found and preferred in a source checkout; nothing →
  the build target; half copy ignored; publish copies, replaces, refuses a source without an index,
  cleans leftovers; `create_app` serves the located bundle; catch-alls after every API route; the
  first-launch build is skipped when a bundle is found; pyproject ships `static/` as artifacts.
- TS: bootstrap parsing (known keys, escaping, junk), plugin injection with a fake backend
  (down / gated / good), the real Vite dev server proxying `/api` HTTP and a WebSocket upgrade to a
  tiny test backend (subprotocol kept), chunk naming, the size report (warn vs CI error).

## Decisions for the lead
- Dev backend override is `INDUS_WEB_DEV_BACKEND` (dev only; read by `vite.config.ts`).
- Release publishing is `python -m indus.web.bundle publish [--source DIR]`; P23's build runs it
  after `pnpm --filter @indus/web build` and before `uv build`.
- The size report fails the build only when `CI` is set; elsewhere it warns, so a user's first
  launch never loses its pages over a size regression.

Compare checklist (filled after the rewrite; reopened `web/vite.config.ts`,
`hermes_cli/main_dashboard.py` (`_resolve_dashboard_web_dist`)):
- [x] Dev page gets the token, embedded-chat flag and initial profile from the running backend
  (ours: every known global) (test: `vite.dev-bootstrap.test.ts::injects the backend's bootstrap
  into its own index.html`; also checked by hand against a real `indus dashboard`)
- [x] Backend unreachable → one warning without any value, page still served (test: `::warns
  without the token when the backend is down, gated or not the dashboard`)
- [x] `/api` proxied with WebSocket upgrades, subprotocol credential intact (test: `::forwards a
  WebSocket upgrade with the subprotocol credential intact`, real Vite server)
- [x] Chunk groups react / xterm / vendor and a 600 kB ceiling (tests: `::names the vendor chunks`,
  `::fails a CI build and only warns elsewhere`; a real build with a 100 kB budget failed as
  expected)
- [x] A missing promised bundle is noticed before serving (ours: first-launch build in a source
  checkout only, JSON 404 naming the build command otherwise) (tests:
  `test_bundle_locate.py::test_first_launch_build_runs_only_in_a_checkout`,
  `::test_first_launch_build_is_skipped_when_pages_exist`)
- Deliberate differences: the reference writes the build straight into the package dir and has an
  env override for caller-managed dists; we keep `apps/web/dist` for development, copy at release
  and have no override. The reference's dev plugin pastes the matched literals; ours parses and
  re-encodes.
- Not taken: the dashboard-plugins dev proxy (L17 is later), `three`/`plot`/`motion` chunk groups
  (we ship none of those libraries; charts are our own SVG, D-16.594).

## Status

Done (2026-10-20). Shared edits as listed under Files.

Integration (wave 32, P18c integrator, 2026-10-21): decisions numbered D-16.655; the real `indus dashboard` serving the checkout's `apps/web/dist` is exercised by every spec in `apps/web/e2e`.

## Wave 46 (builder k, 2026-10-22): F-3 prebuilt pages for a script install

Read: no reference file (F-3 is our own release gap, rehearsal log §5; the reference builds on first
launch, which needs Node and pnpm). Files: `src/indus/web/bundle.py` (`fetch_release`,
`origin_slug`, `missing_pages_hint`, `python -m indus.web.bundle fetch`), `src/indus/web/command.py`
(`_maybe_build` message), `scripts/install/sh/20-runtime.sh` (`ins_web_pages`, rebuilt
`scripts/install.sh`), tests `tests/unit/web/test_bundle_fetch.py`,
`tests/integration/install/test_install_sh_runtime.py` (products cases).

Where the pages are built: by the release workflow, once per release, from the tagged commit; the
script install downloads them. The trust chain starts at what the checkout already holds: HEAD's
newest release tag → its annotated record's `manifest_sha256` → the release's `manifest.json`
(digest equal) → `indus-web-<version>.tar.gz` (digest from the manifest) → unpacked with tarfile's
`data` filter → `publish` into `src/indus/web/static` (gitignored; `locate()` already falls back to
it in a checkout when `apps/web/dist` is not built) with a `.indus-release` stamp. HTTPS only (a
redirect away from HTTPS is refused), manifest capped at 1 MiB, archive at 64 MiB, 60 s per
download. A tree already stamped with HEAD's release is not downloaded again. Exit 3 = HEAD is on
no release (a `main` install builds its own pages); the installer then prints the pnpm command; any
other failure is a warning, never a failed install (the API still works).

Dashboard message without pages and without pnpm: "No prebuilt pages for this checkout, and pnpm
is not on PATH. On a release, run `python -m indus.web.bundle fetch` ... otherwise install Node.js
and pnpm and run `pnpm install && pnpm --filter @indus/web build` in <checkout>."

What `.github/workflows/release.yml` must add (wave-44 integrator owns it; not edited here):
1. in the job that fills the upload directory (before `publish.py upload`): `pnpm install
   --frozen-lockfile`, `pnpm --filter @indus/web build`, then
   `tar -C apps/web/dist -czf <upload-dir>/indus-web-${VERSION}.tar.gz .` (index.html at the
   archive root; `${VERSION}` without the `v`). `publish.py upload` then lists it in
   `manifest.json`, whose digest the release tag records: nothing else changes in publish.py;
2. the artefact check (tooling `test_artifacts` / reference-host scan) should run on that dist as
   it does for the CI one;
3. the rehearsal: a script install of the rehearsal release on a machine without Node shows the
   pages (`indus dashboard` serves `index.html`).

Decisions for the lead:
- W46-k-1 (D-16.1016): the pages are fetched by the Python side (`indus.web.bundle fetch`), not by curl in
  install.sh, so the installer, a manual repair and a later `indus update` share one checked path.
- W46-k-2 (D-16.1017): the archive is verified through the release tag's `manifest_sha256` (the same record
  `publish.py` writes), not a checksum file downloaded beside it; a fork's origin slug is used for
  the download host, the tag record still decides what is accepted.
- W46-k-5 (D-16.1020): the fetch's two local git reads (tags, origin URL) run with an explicit minimal env
  (`GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, `LC_ALL=C`; git found on PATH first), so the
  installer-run module needs no scope and inherits nothing (security_static `inherit_env`).
- W46-k-3 (D-16.1018): the fetched pages go into the runtime tree `src/indus/web/static`, not `apps/web/dist`,
  so a developer build still wins and `indus update`'s pnpm rebuild never mistakes them for one.

Follow-ups (not done, time box):
- `indus update --finish` should refresh the fetched pages for the new release (today they stay
  at the installed release until the installer is rerun or `python -m indus.web.bundle fetch` is
  run; the stamp makes the refresh a no-op when nothing changed). The CLI may not import the web
  layer (D-16.246): run it as `python -m indus.web.bundle fetch` from finish's bundles phase.
- `install.ps1` has the same gap (Windows script install); mirror `ins_web_pages`.

Integration (waves 45 + 46 integrator, 2026-10-22): the release side is a separate job,
`web-pages`, not steps in `upload`: `upload` checks out the default branch and runs only the
release scripts (pinned by `test_script_jobs_never_build_the_default_branch`; R10.24), so the
pages are built from the admitted commit in their own job, which belongs to no lane (it always runs,
like `upload`; `claims.JOB_LANES["web-pages"] = frozenset()`), packs
`indus-web-${VERSION}.tar.gz` (`VERSION` = admit's `version`, no `v`), sweeps `apps/web/dist` and
the tarball with `tooling.checks artifacts --require-ref`, and keeps the tarball as the `web-pages`
workflow artefact; `upload` collects it beside the desktop artefacts and the installers, so
`publish.py upload` lists it in `manifest.json`. The gate needs the job. Pinned by
`tests/unit/ci/test_release_workflow.py::test_every_release_carries_the_dashboard_pages`; the
rehearsal row is in `docs/internals/rehearsal-log.md` §4. Decision D-16.1022.
- Optional: `locate()` could treat stamped pages of another release as stale.

## Wave 47 (builder f, 2026-10-22): `locate()` treats pages of another release as stale

Status: done (2026-10-22), not yet landed (the "Optional" item above).

Read: no reference file. Files: `src/indus/web/bundle.py` (`installed_release`, `locate(...,
release=)`, `BundleLocation.stamped`, kind `stale`, `stale_pages_hint`), `src/indus/web/command.py`
(`_maybe_build` message), tests `tests/unit/web/test_bundle_locate.py` (five cases).

A runtime tree carrying a `.indus-release` stamp (only `fetch_release` writes one) is compared with
the release this install runs: in a checkout, the release HEAD is on, read with the stdlib-only
file probes of `indus.cli.version.checkout_release` (tags, the installer's marker, update receipts;
no git process), `0.0.0` when HEAD is on no release; elsewhere the package's own version. A
different release makes the location `stale`: `found` is False, the directory is where a build
lands (`apps/web/dist` in a checkout), so the old pages are not served, a checkout with pnpm builds
its own on first launch, and without pnpm `indus dashboard` prints "The dashboard pages are from
another release" with how to fetch this release's pages. Unstamped trees (the wheel's, `publish`)
are never judged, nor is any tree when the probe fails.

Fail-first: four of the five new tests fail on the base (`test_unstamped_runtime_pages_are_never_stale`
is a guard and passes on both).

Decisions for the lead:
- W47-f-2 (D-16.1049): stale pages are not served (the server answers "not built" until a fetch or build), not
  served with a warning: pages built for another release talk to an API they were not built for.
- W47-f-3 (D-16.1050): `web.bundle` loads `indus.cli.version` by name (`importlib.import_module`) for the
  checkout's release, the precedent `web.routes.system.install_info` set for the same stdlib-only
  module; no import-linter exemption, and nothing moves. If the lead prefers, the F-4 probes can
  move to `indus.foundation` later and both import them directly.
