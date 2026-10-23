# Task: macOS signing path (P23-15) Phase: 23 Owner: wave-49 builder a

## Status

Done for the agent part (2026-10-23, wave 49, builder a). Hooks, signer, notariser, release entry
point, config wiring, the release workflow's mac job and tests are in place; nothing was signed for
real (no certificate, no network, Linux host). Owed to the owner: the secrets in the protected
`release` environment, the first signed run on a macOS runner and its Gatekeeper assessment
(ACC-S6-1). Changed outside the task's own files: `apps/desktop/scripts/asar.mjs` (header text
reader), `apps/desktop/scripts/dist-unsigned.mjs` (`buildBundles` exported),
`apps/desktop/scripts/builderConfig.test.ts`, `tests/unit/ci/test_release_workflow.py`,
`docs/internals/traps-ledger.md` (R08.22).

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/scripts/after-extract.mjs`
(why the stamp runs on the pristine binary), `apps/desktop/scripts/after-pack.mjs` (darwin branch:
nested signing, digest refresh), `apps/desktop/scripts/mac-sign.mjs` (refresh before the outer
seal), `apps/desktop/scripts/batch-sign-binaries.mjs` (comment header only: the pipeline order),
`apps/desktop/scripts/notarize.mjs` (auth routes, submit, log on rejection, staple retry),
`apps/desktop/electron-builder.config.cjs` (mac section, hook keys), `apps/desktop/electron/entitlements.mac*.plist`
(which keys exist), `apps/desktop/scripts/payload-digests.mjs` (what is refreshed). Closed before
writing. Evidence: none.

What it does (own words): a release build on macOS is signed with the project's Developer ID,
notarised by Apple and stapled, so Gatekeeper opens it without a warning; a build without the
secrets still works and stays unsigned. Four steps, in electron-builder's own order:

1. **after-extract** — before electron-builder copies the app in, computes the ASAR integrity and
   rewrites `Info.plist`, our hook writes the build identity (version, channel, commit from
   `build/desktop-build-stamp.json`) into the pristine bundle's `Info.plist` as `IndusBuildVersion`,
   `IndusBuildChannel`, `IndusBuildCommit`. electron-builder keeps unknown keys when it rewrites the
   file, and everything after this point (integrity, signature) covers the stamped file.
2. **mac.sign** (our signer instead of electron-builder's default) — walks the bundle, signs every
   nested Mach-O file and nested bundle (`.app`, `.framework`, `.xpc`, `.appex`) deepest first,
   with the hardened runtime, a secure timestamp and the inherited entitlements; then refreshes
   the digests the outer signature seals (the `ElectronAsarIntegrity` hashes in `Info.plist`,
   recomputed from each archive's header on disk); then signs the app itself with the app
   entitlements; then `codesign --verify --deep --strict`.
3. **afterSign** — notarises: zips the signed app with `ditto` into a private temp folder, `xcrun
   notarytool submit --wait` with either the Apple ID route (APPLE_ID, APPLE_TEAM_ID,
   APPLE_APP_SPECIFIC_PASSWORD) or the API key route (APPLE_API_KEY, APPLE_API_KEY_ID,
   APPLE_API_ISSUER); a status other than `Accepted` fetches `notarytool log` and fails the build;
   then `xcrun stapler staple` (retried while Apple's ticket service catches up), `stapler
   validate`, and a Gatekeeper assessment `spctl --assess --type execute`.
4. electron-builder then builds the DMG and the ZIP (the update artefact) from the stapled app.

Inputs → outputs: environment (secret names only: MAC_CERTIFICATE, MAC_CERTIFICATE_PASSWORD,
APPLE_ID, APPLE_TEAM_ID, APPLE_APP_SPECIFIC_PASSWORD or APPLE_API_KEY, APPLE_API_KEY_ID,
APPLE_API_ISSUER) → a signed, notarised, stapled `Indus.app` inside `Indus-<v>-mac-<arch>.dmg` and
`.zip`; or, with none set, one line `[sign-mac] unsigned build: …` and the unsigned build of P20-38.

Files: `apps/desktop/scripts/after-extract.mjs`, `apps/desktop/scripts/sign-mac.mjs`,
`apps/desktop/scripts/notarize.mjs`, `apps/desktop/scripts/dist-mac.mjs`,
`apps/desktop/scripts/plist-text.mjs` (the two in-place Info.plist edits),
`apps/desktop/scripts/sign-mac.test.ts`; updated `apps/desktop/electron-builder.config.cjs` (mac
section, `afterExtract` / `afterSign` hook keys), `apps/desktop/resources/entitlements.mac.plist`
(comment), `apps/desktop/scripts/asar.mjs`, `apps/desktop/scripts/dist-unsigned.mjs`,
`.github/workflows/release.yml` (mac job), tests named above.

Edge cases / failure modes:
- No secret set (forks, local builds, a `release` environment not yet filled) → one line naming
  the missing names, unsigned build, exit 0. GitHub passes an unset secret as an empty string:
  empty counts as absent.
- Some but not all names of a route set → the build fails before anything runs, naming the
  missing names (never the values): a half-configured release must not quietly ship unsigned.
- Certificate set but neither notarisation route complete → same failure (a signed but
  un-notarised build fails Gatekeeper anyway).
- Both notarisation routes complete → the API key route wins (no password on a command line).
- APPLE_API_KEY may be a path to the `.p8` file or the key text; text goes to a 0600 file in a
  private temp folder that is removed afterwards, success or not.
- A tool failure message names the tool and its subcommand only; argument lists (which hold the
  password or key path) are never printed or put into an error.
- Symlinks inside frameworks (`Versions/Current`, top-level links) are not followed, so nothing is
  signed twice through a link; a bundle's main executable under `Contents/MacOS/` is sealed by its
  bundle's signature, not signed alone.
- `0xcafebabe` is both a fat Mach-O and a Java class file: treated as Mach-O only when the
  architecture count is small.
- Rejected notarisation → Apple's log in the error; the ticket not yet visible to `stapler` → a
  few retries with growing waits, then the error.
- afterSign also runs for Windows signing (P23-16): the hook returns at once unless the platform
  is darwin.

Our design differences / decisions:
- **W49-a-1** (D-16.1110) Hook files live in `apps/desktop/scripts/`, not `apps/desktop/build/` as the phase
  row says: `build/` is git-ignored (it is the packaging output: `build/app`, `build/release`, the
  stamp), so files there would never be committed. The test is `sign-mac.test.ts` (vitest's include
  is `scripts/**/*.test.ts`).
- **W49-a-2** (D-16.1111) Our own codesign walk instead of `@electron/osx-sign`: the order (nested inside-out,
  digest refresh, outer last) is then ours to test with a fake `codesign` on PATH, and nothing new
  is installed.
- **W49-a-3** (D-16.1112) "Payload digests" for this app are the `ElectronAsarIntegrity` hashes: the app ships
  no runtime payload with its own manifest, and the integrity hashes in `Info.plist` are the
  digests the outer signature seals. They are recomputed from the archives on disk right before the
  outer signature and rewritten only when stale (normally a no-op; it makes any late change to an
  archive fail loudly at load instead of shipping a sealed but wrong hash).
- **W49-a-4** (D-16.1113) All or nothing: no name set → unsigned with one line; a partial set → error with the
  missing names. The certificate is handed to electron-builder under its own names (`CSC_LINK`,
  `CSC_KEY_PASSWORD`) by `dist-mac.mjs` only, which also sets `INDUS_MAC_SIGNING=1`; the config's
  mac `identity` is `null` unless that marker is set, so `dist-unsigned.mjs` stays unsigned.
- **W49-a-5** (D-16.1114) Entitlements stay the three in use: JIT and unsigned executable memory (V8) and audio
  input (voice). No camera (the session refuses camera requests), no
  `disable-library-validation` (the only native code, node-pty, is signed with our own team), no
  calendars/reminders. One file for the app and its nested code.
- **W49-a-6** (D-16.1115) Release entry point `scripts/dist-mac.mjs` (the release workflow's mac job runs it):
  same steps as `dist-unsigned.mjs` but keeps the signing names in its environment. The workflow
  installs dependencies in a step without secrets, so no package install script sees them; the
  build step alone maps the `release` environment's secrets to same-named variables.
- **W49-a-7** (D-16.1116) The DMG itself is not signed or notarised separately: the app inside is stapled, so
  Gatekeeper checks it offline after the DMG is opened; the ZIP (update artefact) is made from the
  stapled app. Notarising the DMG is a follow-up if the owner wants it.
- Gatekeeper's assessment (`spctl`) runs inside the hook after stapling, so a build that would be
  refused never reaches upload; electron-builder's own `gatekeeperAssess` stays off (it runs
  before notarisation).
- Left out: keychain-profile notarisation (`--keychain-profile`), resuming a timed-out
  `notarytool` wait by submission id, Mac App Store (`mas`) builds.

Tests we will write (`apps/desktop/scripts/sign-mac.test.ts`):
- plan: none set → unsigned line naming the names; empty strings count as absent; partial → error
  naming missing names and never a value; API key route preferred.
- walk: Mach-O detection by magic (thin, fat, Java class not Mach-O, text not), symlinks skipped,
  inside-out order, outer app excluded, `Contents/MacOS` executables left to their bundle.
- order (fake `codesign`, `ditto`, `xcrun`, `spctl` on PATH recording argv): every nested target
  signed before the app; digest refresh between the last nested signature and the outer one;
  notarise after the outer signature; staple after an `Accepted` submission; DMG/ZIP untouched.
- digest refresh: a stale `ElectronAsarIntegrity` hash is rewritten to the header's SHA-256; a
  fresh one is left alone.
- notarisation: rejection fetches the log and fails; staple retries; the password and key never
  appear in a log line or an error; key text written 0600 and removed; non-darwin returns.
- after-extract: stamp keys written into the pristine `Electron.app/Contents/Info.plist`; and the
  pinned electron-builder emits `afterExtract` before it computes ASAR integrity and writes
  `Info.plist` (read from its installed source), reading the existing plist rather than a template.
- config: hook keys wired, `identity` null without `INDUS_MAC_SIGNING`, entitlements exactly three.

## Decisions for the lead
- W49-a-1 … W49-a-7 above (D-ids at integration).
- Builder b (P23-16) adds a `win32` stamper to `after-extract.mjs` (`STAMPERS`) and may refactor
  `dist-unsigned.mjs`; this slice only exports `buildBundles` there.

Interop (tool versions): `codesign`, `ditto`, `xcrun notarytool` and `xcrun stapler`, `spctl` from
the macOS runner's Xcode (not pinned; GitHub's `macos-latest` image); electron-builder 26.17.0 hook
order checked against its installed `app-builder-lib` 26.17.0 (`platformPackager.js`,
`electron/electronMac.js`, `macPackager.js`).

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] Stamp runs on the pristine bundle before integrity and the plist rewrite (test: `sign-mac.test.ts::the pinned electron-builder runs afterExtract before ASAR integrity and the Info.plist rewrite`)
- [x] Nested code signed before the app, inside-out (test: `sign-mac.test.ts::signs every nested target before the app, deepest first, and seals refreshed digests`)
- [x] Digests refreshed right before the outer seal (same test; `sign-mac.test.ts::rewrites a stale ElectronAsarIntegrity hash and leaves a fresh one`)
- [x] Notarise after signing, staple after acceptance, log fetched on rejection (tests: `…::notarises after the outer signature and staples the accepted app`, `…::a rejected submission fails with Apple's log`)
- [x] Staple retried while the ticket propagates (test: `…::retries stapling while the ticket is not yet visible`)
- [x] API key given as text goes to a private file that is removed (test: `…::writes key text to a private file and removes it`)
- [x] Missing secrets → skip with one line, unsigned build possible (test: `…::no secret set: unsigned, one line naming the secrets`)
- [x] Gap found: the reference keeps tool argument lists in its error messages; ours name the step only, so a password or key path never reaches a log — pinned by `…::a failing tool's error names the step, not its arguments` and `…::writes key text to a private file and removes it; errors never carry a secret`
- [x] Release lane: secrets only in the build step, from the `release` environment (test: `tests/unit/ci/test_release_workflow.py::test_mac_lane_signs_with_the_release_environment_secrets`)
- Deliberate differences: the reference signs through `@electron/osx-sign` with a per-file callback
  and refreshes a runtime payload manifest; we walk and sign ourselves and refresh the ASAR
  integrity hashes (our app has no payload). The reference's after-extract stamps the Windows exe
  only; ours stamps the macOS bundle (Windows is P23-16's stamper in the same hook). The reference
  notarises only with an API key or keychain profile; we take the Apple ID route too (the secret
  names the owner already has). The reference requests camera, calendars, reminders and
  library-validation-off; we request only what we use. The reference retries stapling only on
  the "record not found" answer; we retry any stapler failure four times (15 s … 120 s) and then
  fail, which costs at most a few minutes on a real error. The reference zips beside the app; we
  zip into a private 0700 temp folder that also holds a key file written from text.
- Not taken: resuming a timed-out notarisation wait by submission id; keychain-profile auth;
  nested Chromium payload repair and wheel-zip signing (no payload here).
