# Wave 49 brief: the last 4 planned tasks (P23-15, P23-16, P24-21, P24-22)

Base: main `a5241f1`. Read in full first: AGENTS.md, HANDOFF.md, docs/plan/01-clean-room-protocol.md, your phase file row
(docs/plan/phases/P23-*.md rows 15/16, docs/plan/phases/P24-*.md rows 21/22) incl. its Traps, Acceptance and Done-when,
docs/plan/phases/LEAD-NOTES.md "P23 wave 36" (release workflow, signing owner items) and "P25 wave 44 landing notes",
docs/internals/release-checklist.md (§0 secrets by name, RM14), the existing apps/desktop/electron-builder.config.cjs and
.github/workflows/release.yml. Clean room: Read → Rewrite → Compare per the protocol; the reference files named in your row are
for reading behaviour only — never copy code; keep the Compare checklist in your task note. Task note first:
docs/notes/P23/<slug>.md or docs/notes/P24/<slug>.md (Status at the top). Decisions labelled `W49-<slice>-<n>`.
TIME BOX about 60 minutes.

Slices (builder id in brackets):
- [a] P23-15 macOS signing path: after-extract stamp before ASAR integrity, sign nested Mach-O inside-out before the app,
  refresh payload digests before the outer signature, hardened runtime + entitlements (only what we use), notarize + staple
  after sign; secrets by NAME only (APPLE_ID, APPLE_TEAM_ID, app-specific password or API key, certificate) — absent →
  skip signing with one line, unsigned build still works. DMG + ZIP. Files per the row; vitest with a fake codesign /
  notarytool recorder proving the order. Wire into electron-builder.config.cjs (mac section ONLY) and the release workflow's
  mac job (secrets from the protected `release` environment, never echoed).
- [b] P23-16 Windows + Linux artefacts: signed per-arch NSIS (D-16.70) via Authenticode — signtool or Azure Trusted Signing
  selected by named secrets, absent → unsigned with one line; executable identity stamped before ASAR integrity; every
  .exe/.dll we ship signed once after stamping; Linux AppImage + .deb unsigned with SHA-256 sums next to them
  (apps/desktop/build/checksums.mjs); update feeds per channel on GitHub Releases; canary has a distinct app id suffix and
  data dir. Edit electron-builder.config.cjs win/linux sections ONLY and the release workflow's win/linux jobs.
- [c] P24-21 Termux APT channel: scripts/termux/build.sh, builder.Dockerfile (base pinned by digest — use a placeholder
  digest marked owner-to-fill if you cannot resolve one offline, and make the pins check flag it), repo.py (static APT
  repo layout, Packages/Packages.gz/Release/InRelease/Release.gpg, pool/), version mapping (v1.2.3 → 1.2.3-1, canary
  v1.2.3+canary.<ts> → 1.2.3~canary.<ts>-1, malformed rejected), unsigned Release → exit 3, install.sh keeps refusing Termux
  and points to the channel, .github/workflows/termux.yml (optional lane, manual/release only). Tests per the row (version
  table, exit 3, golden repo listing) — no docker, no network in tests.
- [d] P24-22 MSIX packaging: apps/desktop/packaging/msix/AppxManifest.template.xml, scripts/release/msix.mjs (+ test):
  per-arch MSIX, signed .msixbundle (named secrets; absent → unsigned with one line), App Installer feed, separate unsigned
  Store bundle with the fixed Store identity built only for stable; Windows 11 22H2+ minimum; canary distinct identity,
  never the Store identity; schema validation of the rendered manifest (vendor a schema only if its licence allows,
  otherwise validate the required structure in code). Store identity values are owner-open: placeholders, never invented.

Rules (ENV RULE), for builder <x>:
- `export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/w49<x>` as ONE statement, then a SECOND
  statement: `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus`.
  Repeat both in every Bash call. TMPDIR unset. Own .venv (`uv sync --locked --offline`).
- node_modules: hard-link from /home/user/indus-agent/node_modules (`cp -al`; same lockfile) if you need TS/JS tools; never
  `pnpm run`, `pnpm exec`, `npx` or `pnpm install`; use ./node_modules/.bin/<tool>.
- No real signing, no secrets, no credential-shaped literals (fake signers record calls); no network; no docker runs; no new
  packages; never pkill by pattern; never git stash; never commit; never write under the real ~, /root or /. Delete build
  output when done. The container may restart; if resumed after one, check what survived and continue.
Before finishing: biome check/format + tsc + vitest for touched JS/TS; actionlint-style self-check of workflow YAML via the
repo's workflow tests (tests/unit/ci); ruff/pyright/lint-imports for touched Python; tooling.checks --require-ref
(security_static, pins, similarity must pass); pytest on touched dirs plus tests/unit/ci, tests/unit/release, tests/contract
with --basetemp=/tmp/w49<x>. Report: files, Done-when status per acceptance line, tests (what each proves), checks,
decisions, owner items (secrets/identities by name), follow-ups.
