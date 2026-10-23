# Task: Windows and Linux desktop artefacts (P23-16) Phase: 23 Owner: wave-49 builder b

## Status

Partial (2026-10-23). Built and tested on Linux with a fake signer: the Windows identity stamp
before ASAR integrity, one Authenticode pass over every `.exe` / `.dll` / `.node` after the fuses,
the installer and uninstaller signed through the same signer, the Linux AppImage + `.deb` with a
SHA-256 sums file, per-channel update feeds and the canary identity. Not run: a real Windows build,
a real signature, a real `.deb` (fpm) — they need a Windows runner, the owner's secrets and the
release lane. Changed outside the row's files: `apps/desktop/electron-builder.config.cjs`
(top-level identity and feed, `afterExtract`, `afterAllArtifactBuild`, win/linux sections),
`apps/desktop/scripts/dist-unsigned.mjs` (`targetsFor` win/linux, channel handed to the config),
new `apps/desktop/scripts/dist-win.mjs` (the Windows release entry, mirroring [a]'s `dist-mac.mjs`),
`apps/desktop/scripts/builderConfig.test.ts` (win/linux and identity expectations),
`.github/workflows/release.yml` (desktop-win and desktop-linux jobs),
`tests/unit/ci/test_release_workflow.py`, `docs/internals/release-checklist.md` §0 (secret names),
`docs/notes/P23/tool-versions.md`.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/scripts/sign-msix.mjs`
(what is signed and when), `apps/desktop/scripts/write-build-stamp.mjs` (stamp contents),
`apps/desktop/electron-builder.config.cjs` (win/linux sections, hook wiring).
Evidence: none.

What it does (own words): the Windows build ships one NSIS installer per architecture (x64, arm64)
built by electron-builder. The executable's identity (version resource: product, description,
copyright, versions, icon) is written into the pristine Electron binary right after extraction —
before electron-builder renames it and embeds the ASAR integrity resource — so nothing rewrites
the PE after that point except the fuse flip and the signature. electron-builder's own resource
edit and signing are switched off (`signAndEditExecutable: false`), so there is exactly one
signer: ours. After the fuses are flipped (afterPack), every `.exe`, `.dll` and `.node` in the
packed folder is signed once; electron-builder then hands the uninstaller and the installer to
the same signer through `win.signtoolOptions.sign`. A ledger keyed by path and content digest
makes a second request for an already signed file a no-op. The signer is chosen from named
secrets, the same names the MSIX path (P24-22) reads: a complete Azure Trusted Signing group
(`AZURE_TRUSTED_SIGNING_ENDPOINT`, `_ACCOUNT`, `_PROFILE`, `INDUS_AZURE_SIGNING_DLIB`; the dlib
authenticates with the vendor's `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`) →
signtool `/dlib`; else a PFX (`WIN_CERTIFICATE` + `WIN_CERTIFICATE_PASSWORD`) → signtool `/f`.
Neither → one line, unsigned build. A partial group stops the build naming the missing names
(never a value). Only `dist-win.mjs` arms the signer: it keeps the values for the process and
deletes them from the environment before electron-builder starts. Linux ships an AppImage and a `.deb`,
unsigned; a `sha256sum`-format sums file is written next to them and lists every Linux artefact.
Every build carries an electron-updater feed for its channel on GitHub Releases (`latest` or
`canary`); a canary build has its own app id (`.canary` suffix), product name (so its own install
folder and Electron data dir), Linux executable and `.deb` package name.

Inputs → outputs: `node scripts/dist-win.mjs` (Windows) / `dist:unsigned` (Linux), env
`INDUS_DESKTOP_CHANNEL` (stable|canary; default canary like the stamp) and the named secrets (the
release workflow passes them to the build step under the same names), optional `INDUS_SIGNTOOL`
and `INDUS_WIN_TIMESTAMP_URL` → `build/release/Indus-Setup-<v>-<arch>.exe` (signed when a signer is
configured), `Indus-<v>-win-<arch>.zip`, `latest.yml` / `canary.yml`; on Linux
`Indus-<v>-linux-<arch>.AppImage`, `Indus-<v>-linux-<arch>.deb`, `latest-linux.yml` /
`canary-linux.yml` and `Indus-<v>-linux-SHA256SUMS.txt`.

Files: `apps/desktop/scripts/sign-win.mjs`, `apps/desktop/scripts/sign-win.test.ts`,
`apps/desktop/scripts/dist-win.mjs`,
`apps/desktop/scripts/checksums.mjs`, `apps/desktop/scripts/checksums.test.ts`,
`apps/desktop/scripts/channel-identity.cjs` (+ the changes listed under Status).

Edge cases / failure modes:
- No secrets → one `[sign-win]` line, nothing signed, the build goes on (local and fork builds).
- Half a group (password without certificate, endpoint without profile …) → the build fails
  with the missing names before anything is built; a complete Azure group with a PFX → Azure.
- `INDUS_AZURE_SIGNING_DLIB` pointing at a missing file → fails before building.
- The same file asked twice (uninstaller handed over again, a rerun of the hook) → signed once.
- A file that changed after we signed it (a new uninstaller at the same temp path) → signed again,
  it is a different binary.
- An `.exe`/`.dll`/`.node` that is not a PE (no `MZ`) → not signed, named in the log.
- signtool exits non-zero → the build fails with the file and exit code (no secret in the text).
- The PFX is written to a fresh private temp folder (0600) and removed at process exit.
- The PE certificate table after signing must sit at the end of the file, 8-byte aligned (checked
  on every signed PE; a later edit that moves it fails the build).
- Linux sums: an artefact missing on disk fails; the sums file never lists itself; names are
  sorted so the file is stable.
- Windows paths: signtool and its args get native paths; no shell is used.

Our design differences / decisions:
- W49-b-1 (D-16.1117): the row names `apps/desktop/build/` for the new modules, but `build/` is the packaging
  output folder (gitignored by the root `build/` rule, skipped by Biome, outside vitest and tsc).
  The modules live beside the other packaging hooks in `apps/desktop/scripts/` with `.test.ts`
  tests, as [a] did (W49-a-1).
- W49-b-2 (D-16.1118): one signer for everything; electron-builder's own resource edit and signing are off
  (`signAndEditExecutable: false`) and its certificate discovery stays off. The secret names are
  the lead's shared set (also P24-22's, W49-d-3). `dist-win.mjs` arms the signer
  (`armWindowsSigning`: keep the values, delete them from the environment) before
  `dist-unsigned.mjs` runs, so neither electron-builder nor its tools see them; `dist:unsigned`
  alone never signs; signtool's child gets no signing value except the Azure credential names.
  The release job mirrors [a]'s mac job: `environment: release`, install step without secrets,
  secrets only in the build step's env under their own names, plain `node`.
- W49-b-3 (D-16.1119): `.node` native modules are DLLs and are signed with `.exe` / `.dll`; vendor-signed
  Electron DLLs are re-signed with ours (one rule: every PE we ship carries our signature once).
- W49-b-4 (D-16.1120): per-arch NSIS (`buildUniversalInstaller: false`), x64 and arm64 in one run on the
  Windows lane; the portable ZIP stays (D-16.84).
- W49-b-5 (D-16.1121): canary identity at config time from `INDUS_DESKTOP_CHANNEL` (dist-unsigned passes the
  stamp's channel): app id `ai.indus.desktop.canary`, product `Indus Canary` (install folder and
  Electron's userData `Indus Canary`), Linux `indus-desktop-canary`, feed channel `canary`. The
  default is canary, as for the stamp: an unlabelled build is never stable.
- W49-b-6 (D-16.1122): update feeds are electron-builder's `github` provider for `RELEASE_REPOSITORY`, read
  from `electron/releaseBase.ts` (the desktop's one copy), so no new copy of the owner value.
  Windows keeps `verifyUpdateCodeSignature: false` and the manual update owner (the stamp) until a
  signed build's publisher name is known; turning on the Windows updater is a follow-up.
  Since wave 50 e (D-16.1171 – D-16.1173) Windows update signature verification is on
  (`verifyUpdateCodeSignature: true`, `win.signtoolOptions.publisherName` read from
  `packaging/msix/identity.json`; a signed build refuses the `OWNER-TO-FILL` publisher); the
  update owner stays `manual` until the first signed run.
- W49-b-7 (D-16.1123): the `.deb` needs a maintainer; we use the RM12 placeholder
  `Indus Agent maintainers <maintainers@indus.invalid>` until the owner picks an address.
- W49-b-8 (D-16.1124): the Azure route uses signtool `/dlib` with the Trusted Signing client package (fetched
  by the Windows lane on every run, `continue-on-error`, no secret in that step;
  `INDUS_AZURE_SIGNING_DLIB` is set in the build step only when the Azure endpoint secret exists;
  version 1.0.53 in tool-versions.md, to verify on the first signed run).
- W49-b-9 (D-16.1125): the Windows stamp is shaped as an [a] `after-extract.mjs` stamper
  (`stampWindowsExecutable(context, stamp)`) and, given the build stamp, adds `IndusBuildVersion`,
  `IndusBuildChannel`, `IndusBuildCommit` to the version resource like the macOS Info.plist keys.
  Merge: replace this slice's `afterExtract` in the config with [a]'s and add
  `win32: (context, stamp) => stampWindowsExecutable(context, stamp)` to `STAMPERS`.
- Left out: MSIX/Store (P24-22, builder d), Linux package signing (row: unsigned), Squirrel.

Tests we will write:
- signer selection: none / PFX / Azure / Azure wins / partial groups, names only in errors; arming
  removes the values from the environment.
- signtool argv for both routes; secrets never in a log line; Azure creds only in the child env.
- ledger: every `.exe`/`.dll`/`.node` signed once; second request no-op; changed file re-signed;
  non-PE skipped.
- order: the stamp hook runs on `electron.exe` before integrity; signing only after the fuses
  (recorder over the config's afterPack with a fake packager); installer/uninstaller through the
  same signer.
- PE certificate table check on synthetic PE buffers.
- checksums: lists every Linux artefact, format `sha256sum -c` accepts, never itself, missing fails.
- config: canary vs stable identity, feeds per channel, NSIS per arch, AppImage + deb.
- workflow (pytest): desktop-win uses the `release` environment and only the named secrets in the
  build step; desktop-linux takes no secret.

## Decisions for the lead
- W49-b-1 … W49-b-8 above (each with how to change it).
- Owner: the Windows secrets by name (§0 of the release checklist), the publisher name for update
  signature checks, and the maintainer address (RM12) for the `.deb`.

Compare checklist (filled after the rewrite; reopened `sign-msix.mjs`, `write-build-stamp.mjs`,
`electron-builder.config.cjs`):
- [x] The reference signs one artefact per build (its MSIX package) and leaves the inner binaries
  to the package's block map; we ship NSIS, which has no block map, so every inner PE is signed
  (Authenticode) — deliberate (D-16.70). (test: `sign-win.test.ts` › "signs every PE once")
- [x] The reference stamps the executable identity in after-extract, on the pristine binary,
  because the integrity rewrite later breaks the resource editor — same order here. (test:
  `sign-win.test.ts` › "stamps electron.exe before the rename and integrity")
- [x] Azure settings come from the environment, not the config — same idea, our shared names,
  and the vendor credential names only in signtool's child env. (test: "Azure route")
- [x] A Store-prefixed artefact is never signed in the reference; we build no Store artefact here
  (P24-22 owns MSIX/Store).
- [x] The reference's feed is GitHub per channel when no mirror bucket is set; we have no mirror
  (R2 legs absent, D-16.58), so GitHub per channel always. (test: `builderConfig.test.ts` › feeds)
- [x] The reference's stamp records commit, branch, dirty and source; ours (P20-38) already records
  version, commit, channel and update owner and refuses a checkout other than the admitted commit;
  nothing added. Not taken: the dirty flag (release lanes build a clean admitted checkout).
- Deliberate differences: per-arch NSIS + ZIP instead of MSIX; AppImage + deb instead of AppImage
  only; one signer selected by named secrets instead of an Azure-only hook; signed-once ledger.
- Not taken: the Light/bundled/store variants, the relaunch waiter resource, channel builds with a
  build id (our channels are stable and canary only).

## Integration (wave 49)

- W49-b-9 merged as asked (D-16.1144): the config has one `afterExtract` that runs
  `after-extract.mjs`, whose `STAMPERS` hold `darwin` (P23-15) and `win32`
  (`stampWindowsExecutable(context, stamp)`), so the Windows version resource now carries the three
  build values too; `afterSign` (notarisation) sits beside `afterPack` and `afterAllArtifactBuild`.
  `sign-win.test.ts`'s wiring test now checks `STAMPERS.win32` instead of the config's hook text. The
  top-level canary identity also applies to macOS (`Indus Canary.app`). The `release.yml` comment
  over the desktop lanes was rewritten once for both signing lanes.
