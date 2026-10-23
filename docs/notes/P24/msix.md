# Task: MSIX packaging (P24-22) Phase: 24 Owner: wave 49 builder d (ops)

Status: Partial (2026-10-23) — packaging script, manifest template, identities and tests built and
green on Linux with recorded Windows tools; no real `makeappx`/`signtool` run (no Windows host here),
no release-workflow job (W49-d-6), Store identity and signing publisher owner-open. Details at the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/msix-shared.mjs` (all:
version quads, template macro, identity selection), `scripts/bundle-store-msixbundle.mjs` (all),
`scripts/stage-msixbundle.mjs` (all: bundle staging, signing, retries; read because the Store
script defers to it for the out-of-store half), research 10 §3.15 and §3.16 lane list.
Evidence: none (packaging behaviour is fully visible in the scripts above).

Interop pins (vendor schemas, names spelled as Microsoft does): package manifest namespaces
`http://schemas.microsoft.com/appx/manifest/foundation/windows10`, `…/uap/windows10`,
`…/foundation/windows10/restrictedcapabilities`, `…/desktop/windows10/6`; App Installer file schema
`http://schemas.microsoft.com/appx/appinstaller/2021`; Windows 11 22H2 = `10.0.22621.0`
(`TargetDeviceFamily Windows.Desktop`), tested up to `10.0.26100.0` (24H2). Tools: `makeappx pack`,
`makeappx bundle`, `signtool sign` / `signtool verify /pa` from the Windows SDK. The SDK's XSDs ship
under the SDK licence, which does not allow redistribution, so they are **not** vendored: the
rendered manifest and feed are checked in code against the structure those schemas require (W49-d-4).

What it does (own words): on a Windows release runner, after electron-builder has produced the
unpacked x64 and arm64 apps, `scripts/release/msix.mjs` lays each one out with a rendered
`AppxManifest.xml` and the tile images, packs one `.msix` per architecture, bundles them into one
`.msixbundle` (signed when the signing secrets are present, otherwise unsigned with one line on
stderr), and writes an App Installer file (`.appinstaller`) that points at the bundle on GitHub
Releases so installed copies update themselves. For a stable release it can also build a separate
Store bundle: the same layouts with the fixed Store identity, never signed (the Store signs on
ingestion), refused for anything that is not a stable release. Canary builds get their own package
name, display name and feed, and can never be given the Store identity. Windows 11 22H2 is the
minimum; older Windows refuses to install. Without this the Windows app ships only as NSIS
(D-16.70), which is fine for 1.0 (D-16.15 keeps MSIX optional).

Inputs → outputs:
- `node scripts/release/msix.mjs build --version X.Y.Z[-canary.STAMP] [--arch x64,arm64]
  [--unpacked-x64 DIR] [--unpacked-arm64 DIR] [--out DIR] [--store]` → `Indus-<quad>-<arch>.msix`
  per arch, `Indus-<quad>.msixbundle` (canary: `Indus-Canary-…`), `Indus.appinstaller`
  (canary: `Indus-Canary.appinstaller`), with `--store` also `Indus-Store-<quad>.msixbundle`;
  a JSON summary on stdout (`files`, `signed`, `store`).
- `node scripts/release/msix.mjs render --version … [--arch …] [--store]` → the rendered manifest
  on stdout (validated), for review without the SDK.
- Environment: `INDUS_MAKEAPPX`, `INDUS_SIGNTOOL` (tool paths; default the names on PATH),
  signing secrets by name only (W49-d-3), `INDUS_SIGN_TIMESTAMP_URL` (optional override).

Files: `apps/desktop/packaging/msix/AppxManifest.template.xml`,
`apps/desktop/packaging/msix/identity.json`, `scripts/release/msix.mjs`,
`scripts/release/msix.test.mjs`, `tests/unit/release/test_msix_node.py` (runs the node tests
inside the Python gate), this note.

Edge cases / failure modes:
- version not of our grammar (`X.Y.Z` or `X.Y.Z-canary.YYYYMMDDTHHMMSSZ`), a field over 65535, an
  impossible calendar stamp → refused before anything is written;
- `--store` with a canary version → refused ("Store bundle is for stable releases only");
- Store identity still holding the owner placeholder → Store build refused naming the file;
- a canary manifest given a Store name or publisher → refused by the renderer;
- signing secrets partly set (one of a group) → refused naming the missing names, never a silent
  unsigned build; none set → unsigned, one line;
- signing with the placeholder publisher → refused: Windows rejects a package whose publisher is not
  the certificate subject, so the run would only fail later and less clearly;
- `signtool sign` flaky on the timestamp server → three attempts, then fail; `signtool verify /pa`
  after the bundle is signed;
- an unpacked folder missing, or without the manifest's executable → refused before packing;
- `makeappx bundle /d` takes every package in the folder → only this run's per-arch packages are
  copied into a fresh staging folder (and the Store packages into a separate one);
- a template placeholder left unfilled, or a value containing XML specials → unfilled is refused,
  values are escaped;
- the PFX secret is written to a 0600 temp file and removed in `finally`; the password never
  appears in printed command lines (redacted).

Our design differences / decisions:
- Names: `Indus` file prefix and display name, package names `IndusAgent.Desktop` and
  `IndusAgent.Desktop.Canary`, deep-link scheme `indus` for both (W49-d-8).
- Version quad (W49-d-1): stable `X.Y.Z.0` (semver order, Store-legal revision 0); canary
  `X.Y.H.L` where `H.L` are the seconds since 2026-01-01T00:00:00Z of the canary stamp split into
  two 16-bit halves (`H = t >> 16`, `L = t & 0xffff`), so canaries order by build time and a newer
  minor sorts above every older canary. The reference derives both quads from the clock; ours keeps
  stable equal to the product version, which the Store accepts as long as each submission rises.
- Per-arch packages are made by us with `makeappx pack` from electron-builder's unpacked folders,
  so the electron-builder config (builder b's win/linux sections) is untouched (W49-d-2).
- Signing (W49-d-3): Azure Trusted Signing when `AZURE_TRUSTED_SIGNING_ENDPOINT`,
  `AZURE_TRUSTED_SIGNING_ACCOUNT`, `AZURE_TRUSTED_SIGNING_PROFILE` and `INDUS_AZURE_SIGNING_DLIB` are
  set (the dlib reads `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` itself); else a PFX
  from `WIN_CERTIFICATE` (base64) + `WIN_CERTIFICATE_PASSWORD` (the names already in the release
  checklist). Per-arch packages are signed before bundling and the bundle after, so a per-arch
  `.msix` can also be installed alone.
- Schema validation in code (W49-d-4): a small XML reader of our own plus a rule table for the
  elements and attributes we emit.
- File-system and registry write virtualization are off (`desktop6`, with the restricted
  `unvirtualizedResources` capability, W49-d-5): the app and the daemon it starts share `~/.indus`
  and `%LOCALAPPDATA%` with the CLI, which a redirected per-package copy would split.
- No release-workflow job in this slice (W49-d-6).
- Left out: one-off (commit) builds and their red icon branding, the reference's channel-request
  bridge, content types on upload (GitHub serves assets as it likes; App Installer reads the file
  after download).

Tests we will write:
- version quad table (stable, canary, malformed, >65535, impossible stamp, ordering);
- manifest per channel (stable vs canary identity, never the Store identity on canary, Win11 22H2
  minimum, every placeholder filled, escaping) and the in-code schema check catching seeded faults;
- Store bundle refused for canary and with the placeholder identity; built unsigned with a filled
  identity even when signing secrets are present;
- signing selection (none → unsigned one line, partial → refused, PFX, Azure), sign order (per-arch,
  then bundle, then verify), retry then fail, password redacted, temp PFX removed;
- bundle staging holds only this run's packages; App Installer feed shape and URLs per channel;
- the CLI end to end with fake `makeappx`/`signtool` scripts (POSIX only).

## Decisions for the lead
- W49-d-1 (D-16.1134) Version quads as above; change in `packageVersion()` in `scripts/release/msix.mjs`.
- W49-d-2 (D-16.1135) Per-arch MSIX packed by our script from the unpacked app, not electron-builder's `appx`
  target; keeps MSIX out of `electron-builder.config.cjs`.
- W49-d-3 (D-16.1136) Signing secret names (align with builder b's at integration if b chose others).
- W49-d-4 (D-16.1137) No vendored XSD (SDK licence); structural validation in code.
- W49-d-5 (D-16.1138) Write virtualization off (restricted capability `unvirtualizedResources`; the Store asks
  for a justification at submission).
- W49-d-6 (D-16.1139) No `release.yml` job: every job there must be a lane in `scripts/release/claims.py`
  (`JOB_LANES`, the gate's `needs`, `DEFAULT_LANES` = all lanes), so an optional job means an opt-in
  lane mechanism. Wiring sketch for later: a `desktop-msix` job on `windows-latest` after
  `desktop-win` (unpacked folders kept as an artifact), `environment: release`, secrets above,
  `node scripts/release/msix.mjs build --version "$VERSION" ${store:+--store}` and the outputs added
  to `upload`.
- W49-d-8 (D-16.1141) Canary registers the same `indus` link scheme as stable (the desktop registers and routes `indus` today);
  with both installed Windows asks which app opens the link. A canary-only scheme needs the app to register and route it too (`identity.json` `canary.protocol`).
- W49-d-9 (D-16.1142) A Store bundle needs a 1.0.0 or later version (non-zero major), checked before packing.
- W49-d-7 (D-16.1140) Feed URLs: stable `https://github.com/<RELEASE_REPOSITORY>/releases/latest/download/Indus.appinstaller`
  (works with no extra step once the file is a release asset); canary
  `…/releases/download/channels/Indus-Canary.appinstaller`, on the fixed `channels` release that
  already holds `channels.json` (`CHANNELS_TAG`), because `latest` never points at a prerelease.
  The bundle URL names the release's own tag (`v<version>`). `<RELEASE_REPOSITORY>` is read from
  `src/indus/foundation/versions.py`, so the D-16.690 change stays one edit. Follow-up: the publish
  step re-uploads the canary feed onto `channels`.

Compare checklist (filled after the rewrite; reopened `scripts/msix-shared.mjs`,
`scripts/bundle-store-msixbundle.mjs`, `scripts/stage-msixbundle.mjs`):
- [x] Four 16-bit fields, Store revision 0 (test: `msix.test.mjs` "package version quads" — table,
  ordering, malformed / over 16 bits / impossible stamp refused).
- [x] One fill per template field, nothing left unfilled (test: "escapes values and refuses unfilled or
  unused fields"; seeded "unfilled field" in the schema suite).
- [x] Store bundle only for a stable release (tests: "refuses a Store bundle for anything but a stable
  release"; CLI "exits 1 on a Store request for a canary").
- [x] Store bundle never signed, Store packages kept out of the sideload bundle and the other way round
  (test: "builds the Store bundle unsigned … even when signing is configured" — inputs of each bundle).
- [x] `makeappx bundle /d` sees only this run's packages (test: "packs per arch, bundles only this
  run's packages" — a stray `.msix` in the output folder stays out).
- [x] Bundle version passed with `/bv` (same test).
- [x] Signing needs a timestamp, is retried, then verified with `/pa`; no secrets → unsigned with one
  line (tests: "signs each package, then the bundle, then verifies", "retries a failed sign up to three
  times", "packs per arch … unsigned with one line").
- [x] Publisher must be the signing subject — reference hard-codes it; ours refuses a placeholder
  (test: "refuses to sign with the placeholder publisher").
- [x] Store package needs a non-zero major — gap found at Compare, fixed by a check in `build()`,
  pinned by "refuses a Store bundle before 1.0.0".
- [x] Canary identity distinct, never the Store one (test: "never gives a canary the Store identity").
- Deliberate differences: the reference derives both quads from the clock (stable from the tag's
  creation time, canary as `yy.mmdd.hh.mmss`); ours keeps stable equal to the product version and
  counts canary seconds from 2026-01-01 (W49-d-1). The reference signs only the bundle and lets
  signtool re-sign the inner packages; ours signs each package first, so a single-arch `.msix` can be
  installed alone. The reference's feed is written by a separate publisher; ours is written here and
  checked by `checkFeed`. Tool discovery (SDK download) is the runner's job here (`INDUS_MAKEAPPX`,
  `INDUS_SIGNTOOL`, `INDUS_AZURE_SIGNING_DLIB`).
- Not taken: the canary release-epoch cross-check (our `INDUS_RELEASE_EPOCH` is the claim's tagger
  time, not the canary stamp, so the check would refuse every real canary); commit (one-off) builds
  and their icon branding; app execution aliases for CLI launchers (the desktop package does not
  carry the CLI; `indus` comes from the installer); content types for the feed and bundle uploads;
  the reference's channel-request bridge.

## Status

Partial (2026-10-23). Built: the manifest template and identities under
`apps/desktop/packaging/msix/`, `scripts/release/msix.mjs` (`build`, `render`), 49 `node --test` cases
in `scripts/release/msix.test.mjs`, run inside the Python gate by
`tests/unit/release/test_msix_node.py`. Nothing outside the task's files changed. Open: a first real
run on a Windows runner (makeappx/signtool, install on Windows 11 22H2 and on 24H2, an update through
the App Installer file), the release-workflow job (W49-d-6), the publish step for the canary feed on
the `channels` release (W49-d-7), and the owner items: the signing certificate subject for
`stable.publisher` / `canary.publisher`, the Partner Center identity (`store.identityName`,
`store.publisher`, `store.publisherDisplayName`), and the signing secrets by name
(`WIN_CERTIFICATE`, `WIN_CERTIFICATE_PASSWORD`, or `AZURE_TRUSTED_SIGNING_ENDPOINT`,
`AZURE_TRUSTED_SIGNING_ACCOUNT`, `AZURE_TRUSTED_SIGNING_PROFILE` with `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, plus the dlib path `INDUS_AZURE_SIGNING_DLIB`). MSIX stays
after 1.0 unless the owner moves it (D-16.15, RL9).

## Integration (wave 49)

- W49-d-6 resolved at the integration (D-16.1143): `desktop-msix` is an optional lane in
  `scripts/release/claims.py` (off unless named; naming it without `desktop-win` is refused,
  `LANE_REQUIRES`). When it is on, `desktop-win` keeps `win-unpacked` / `win-arm64-unpacked` as a
  `win-unpacked` artifact (a name the upload's `desktop-*` pattern never collects) and the
  `desktop-msix` job (`windows-latest`, `environment: release`, needs `desktop-win`) packs those very
  apps: Windows SDK `bin` on PATH, the Trusted Signing client, then
  `node scripts/release/msix.mjs build --version "$VERSION" --out "$RUNNER_TEMP\msix"` with the
  Windows secret names in that step's env only; the bundle and the App Installer feed are kept as the
  `desktop-msix` artefact, which `upload` collects onto the draft. No `--store` in the lane (W49-d-9:
  the Store needs 1.0.0+; a manual step). W49-d-3: the secret names already agree with
  `sign-win.mjs` (P23-16). Never run; the canary feed's re-upload onto `channels` (W49-d-7) is still
  a follow-up.
