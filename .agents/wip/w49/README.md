# Wave 49 parked patches (base 057905d): the last 4 planned tasks. Integrator assigns D-16 numbers to W49-<slice>-<n>.

## w49d.patch: P24-22 MSIX packaging (built; not run on Windows; no release job — W49-d-6)
- 6 new files: apps/desktop/packaging/msix/{AppxManifest.template.xml, identity.json (publisher/Store values OWNER-TO-FILL)},
  scripts/release/msix.mjs (build: makeappx pack per arch → sign → bundle → sign + verify → .appinstaller; --store unsigned
  Store bundle, stable ≥1.0.0 only; render), scripts/release/msix.test.mjs (49 node --test cases), tests/unit/release/
  test_msix_node.py (runs them in the Python gate), docs/notes/P24/msix.md.
- Done-when: per-arch MSIX, signed bundle (unsigned with one line without secrets; partial secret group refused), App
  Installer feed (stable latest/, canary on the `channels` release), Store bundle stable-only + never canary identity,
  Win 11 22H2 minimum, manifest validated in code (SDK XSDs not redistributable). Real Windows run: owed.
- Secret names (W49-d-3; builder b asked to use the same): WIN_CERTIFICATE + WIN_CERTIFICATE_PASSWORD, or
  AZURE_TRUSTED_SIGNING_{ENDPOINT,ACCOUNT,PROFILE} + INDUS_AZURE_SIGNING_DLIB (+ AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET).
- Checks: node --test 49; pytest release/ci/packaging/contract 2236; tooling.checks ok; biome; tsc checkJs; ruff/pyright.
- W49-d-1..9. Follow-ups: desktop-msix opt-in lane in claims.py/JOB_LANES (sketch in the note), canary feed re-upload,
  real Windows install/update on 22H2 and 24H2. Owner: publishers, Partner Center identity, signing secrets, MSIX pre-1.0?

## w49a.patch: P23-15 macOS signing path (built; first signed run on macOS is the owner's)
- 15 files, +1502/-24. New apps/desktop/scripts/{after-extract.mjs (STAMPERS map; stamp into the pristine Info.plist before
  ASAR integrity), sign-mac.mjs (Mach-O walk deepest first → codesign nested → refresh ElectronAsarIntegrity → sign app →
  verify), notarize.mjs (ditto → notarytool submit --wait → staple w/ retries → stapler validate → spctl), dist-mac.mjs
  (release entry; no secrets → unsigned with one line; partial → refused), plist-text.mjs, sign-mac.test.ts (19)};
  electron-builder.config.cjs mac section + top-level afterExtract/afterSign; release.yml desktop-mac job
  (environment: release, secrets only in the build step env, plain node); test_release_workflow.py; traps R08.22.
- W49-a-1: hooks live in apps/desktop/scripts/ (apps/desktop/build/ is git-ignored). W49-a-2..7 in docs/notes/P23/sign-mac.md.
- Checks: biome, tsc, desktop vitest 1636; ruff/pyright; pytest ci/release/contract 2066; tooling.checks ok.
- Owner: secrets MAC_CERTIFICATE, MAC_CERTIFICATE_PASSWORD, and APPLE_ID + APPLE_TEAM_ID + APPLE_APP_SPECIFIC_PASSWORD or
  APPLE_API_KEY + APPLE_API_KEY_ID + APPLE_API_ISSUER (update RM14 / §0). DECISION: the mac job now uses the `release`
  environment — if it requires reviewers, every canary waits at the mac lane. ACC-S6-1 needs the first signed run.
- MERGE with w49b: top-level hook keys, after-extract STAMPERS (win32), builderConfig.test.ts, test_release_workflow.py
  loop, traps R08.22 row, release.yml shared desktop-lanes comment (reword once both land).
