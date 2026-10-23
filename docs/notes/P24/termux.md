# Task: Termux APT channel (P24-21) Phase: 24 Owner: wave-49 builder c

## Status

Partial (2026-10-23). Built and tested here: the version mapping, the static APT repository
builder (signing seam, real-gpg end to end, exit 3 when unsigned), the stage plan and refusals of
`build.sh` (dry run and fake tools), the builder Dockerfile (statically; its base digest is an
owner-to-fill placeholder the pins check reports as a warning), the install.sh pointer, a Termux
section in the install guide and the optional `termux.yml` lane (shape tests). Never run here (no
docker, no network, no aarch64 host): `build.sh` stages 3–8 and the lane itself — the first lane
run is their test. Owner items: the base digest, the signing key secrets, where the repository is
hosted (its URL and key fingerprint go into the install guide). Outside the task's own files:
`scripts/install/sh/00-core.sh` + generated `scripts/install.sh` and its test,
`docs/user/getting-started/install.md`, `tooling/checks/pins.py` (rule 6) + its tests, and the
grammar callers list in `tests/unit/release/test_versioning.py`.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/termux/termux_build.sh`
(stage order: host half then container half, tag gate, pinned image, payload interpreter),
`scripts/termux/build_deb.sh` (payload checks, version derivation, control file, on-device
prefix layout, xz members, validation run), `pm/termux_libs.py` (skim: the rolling Termux pool
and why runtime libraries are pinned), `scripts/termux/deb_version.py` (the mapping table),
`scripts/termux/stage_apt_repo.py` (repo layout, Release fields, exit 3, isolated gpg home,
post-sign verification, immutability of published versions), `scripts/termux/check_apt.sh`
(the on-device install proof), `scripts/termux/termux-builder.Dockerfile` and `build_config.sh`
(base by build arg, uid 1000, toolchain list, platform tag and ABI), `.github/workflows/
termux-verify.yml` (skim: callable lane, skipped without its secrets), `scripts/install.sh`
(the Termux refusal line only); research 10 §3.12. Ours: `src/indus/foundation/versions.py`
(the one tag grammar), `scripts/install/sh/00-core.sh`, `tooling/checks/pins.py`,
`docker/Dockerfile` (how our images pin a base). Closed before writing.
Evidence: none (packaging scripts; no running reference needed).

Protocol pins: Debian policy (current) §5.6.12 version ordering; the Debian repository format
(`dists/<suite>/Release`, `InRelease` clearsigned, `Release.gpg` detached, `main/binary-<arch>/
Packages[.gz]`); PEP 738 Android platform tag `android_24_arm64_v8a`.

What it does (own words): Termux users on Android (aarch64 only) get Indus Agent from a signed
static APT repository instead of `install.sh`, which keeps refusing Termux and now names the
channel. A release tag maps to a Debian version: stable `v1.2.3` → `1.2.3-1`; a canary
`v1.2.4-canary.<YYYYMMDDTHHMMSSZ>` → `1.2.4~canary.<stamp>-1`, where `~` sorts the canary
below its own stable in dpkg order; anything the product grammar rejects is refused. The build
runs in a builder image pinned by digest, stages bionic CPython, Node and uv, builds the wheels
and retags them `android_24_arm64_v8a` (ABI `cp314`), packs one `.deb` for aarch64, then
`repo.py` assembles the repository: `.deb`s copied into `pool/`, `Packages` and `Packages.gz`,
a `Release` with SHA-256 sums, and when a signing key is given `InRelease` + `Release.gpg`
(verified after signing) and the public key `key.asc`. With no key the Release is written but
the build exits 3, which CI treats as failure: an unsigned channel is never published. The lane
is optional: manual dispatch or a call from the release workflow, never on push.

Inputs → outputs:
- `repo.py version <tag>` → the Debian version (exit 0) or a message (exit 2, malformed).
- `repo.py channel <tag>` → `stable` / `canary`.
- `repo.py build --debs DIR --out DIR --tag TAG [--key-file F] [--date RFC2822-UTC]` →
  `out/dists/indus-<channel>/{Release, InRelease, Release.gpg, main/binary-aarch64/{Packages,
  Packages.gz}}`, `out/pool/main/i/indus-agent/indus-agent_<v>_aarch64.deb`, `out/key.asc`;
  exit 0 signed, 3 unsigned, 2 bad input.
- `build.sh --tag TAG --out DIR [--source DIR] [--key-file F] [--dry-run]` → the stages in
  order; `--dry-run` prints the plan and runs nothing.

Files: `scripts/termux/__init__.py`, `scripts/termux/build.sh`, `scripts/termux/builder.Dockerfile`,
`scripts/termux/repo.py`, `tests/unit/release/test_termux_versions.py`,
`tests/unit/release/test_termux_repo.py`, `tests/unit/release/test_termux_build_script.py`,
`tests/unit/ci/test_termux_workflow.py`, `.github/workflows/termux.yml`; changed:
`scripts/install/sh/00-core.sh`, `scripts/install.sh` (generated),
`tests/integration/install/test_install_sh_core.py`, `docs/user/getting-started/install.md`,
`tooling/checks/pins.py`, `tooling/checks/tests/test_pins.py`,
`tests/unit/release/test_versioning.py` (callers list).

Edge cases / failure modes:
- Malformed tags (leading zeros, missing `v`, four-digit major, impossible stamps, the
  reference's `+canary` spelling, whitespace) → refused, exit 2, nothing written.
- A `.deb` whose control lacks Package/Version/Architecture, whose architecture is not
  `aarch64`, whose name or version could escape the pool path, or whose control member is
  compressed with something we cannot read → refused (exit 2).
- A `.deb` whose control version differs from the tag's mapped version → refused (a build
  that packed the wrong version must not be published under this tag).
- Re-running with a version already in the suite: identical bytes → no-op; different bytes →
  refused (published packages are immutable).
- No key, or no `gpg` → Release written, stale `InRelease` / `Release.gpg` from an earlier run
  removed (a stale signature never sits next to a new Release), exit 3.
- A key file holding several secret keys → refused (no guessing which key signs).
- Signatures checked after signing (VALIDSIG for the imported fingerprint) or the run fails.
- The passphrase reaches gpg on stdin, never in argv; gpg runs in a throwaway home.
- Decompression of control members is bounded (a hostile `.deb` cannot balloon memory).
- `build.sh` refuses a placeholder base digest, a non-aarch64 host and missing tools before any
  container runs.

Our design differences / decisions:
- **W49-c-1** (D-16.1126) Canary tag spelling: our grammar (P23-21, `indus.foundation.versions`) spells a
  canary `v1.2.4-canary.<stamp>`; the row's `v1.2.3+canary.<stamp>` is the reference's
  spelling and our grammar rejects it. The mapping uses our grammar (imported, never
  re-typed): `v1.2.4-canary.<stamp>` → `1.2.4~canary.<stamp>-1`, and `+canary` is in the
  malformed table. Our canary is the next patch's prerelease, so it sorts above the previous
  stable and below its own stable in dpkg order too (tested with our comparator and, when
  present, `dpkg --compare-versions`).
- **W49-c-2** (D-16.1127) Suites are `indus-stable` and `indus-canary`, component `main`, architecture
  `aarch64`; package name `indus-agent`. Standard repository layout
  (`dists/<suite>/main/binary-aarch64/Packages`) and a Debian-style pool path
  `pool/main/<initial>/<package>/<package>_<version>_<arch>.deb` built from the control fields,
  never from the input file name.
- **W49-c-3** (D-16.1128) The repository accumulates: an existing `Packages` in the output suite is read
  back so canary and stable versions live side by side; stanzas sort by package then dpkg
  version (our own comparator, Debian policy §5.6.12).
- **W49-c-4** (D-16.1129) Exit codes: 0 signed, 2 bad input or refused, 3 Release unsigned.
- **W49-c-5** (D-16.1130) Base image placeholder: no network here, so `builder.Dockerfile` carries
  `sha256:` + 64 zeros marked `owner-to-fill`; `build.sh` refuses to build with it, and the pins
  check (new rule 6: every Dockerfile base outside `tests/` is pinned by digest) reports it as a
  warning (not a failure) so the guard stays green while the owner item is visible.
- **W49-c-6** (D-16.1131) Runtimes: the builder image installs Termux's own bionic `python`, `nodejs-lts`
  and `uv` builds and the stage copies their file lists into the payload (`dpkg -L`), recording
  each package version in a build manifest; wheels are built inside the container and retagged
  with `wheel tags` (pinned in the image). This replaces the reference's source builds from a
  package-manager lock (we have no bionic rows in our `indus pm` lock yet).
- **W49-c-7** (D-16.1132) The lane: `termux.yml` runs on `workflow_dispatch` (a tag) and `workflow_call`
  only; it is not wired into `release.yml`'s lane table in this wave because builders a/b edit
  that workflow; the integrator adds an off-by-default `termux` lane (follow-up). The signing
  key comes from the protected `release` environment by name (`TERMUX_APT_SIGNING_KEY`,
  `TERMUX_APT_SIGNING_PASSPHRASE`); without it the build exits 3 and the lane fails.
- **W49-c-8** (D-16.1133) The `.deb` stamps `.install_method` = `apt`; `indus update` reads an unknown stamp
  as a package install and refuses with the package message. A Termux-specific message
  (`pkg upgrade indus-agent`) is a follow-up in `cli/update`.
- Left out: `by-hash` index copies (`Acquire-By-Hash`), MD5/SHA1 sums (apt uses SHA-256), the
  reference's commit-only builds, its wheelhouse cache and its R2 mirror of pinned inputs.

Tests we will write:
- Version table (stable, canary, malformed) and the channel; the canary sorts between the
  previous stable and its own stable in dpkg order; the mapping imports the one grammar.
- Repository golden listing and golden `Packages`/`Release` text with a fake signer; exit 3
  unsigned (and stale signatures removed); refused inputs; immutability; idempotent rerun;
  accumulation of stable + canary; bounded decompression; gpg signer argv never carries the
  passphrase and a fake gpg's VALIDSIG mismatch fails.
- `build.sh --dry-run` stage order; malformed tag exit 2; placeholder digest refusal outside
  dry run; the Dockerfile's base is a digest ARG and the pins check flags the placeholder.
- `termux.yml` shape: manual/call triggers only, SHA-pinned actions, `release` environment,
  secrets by name, exit 3 treated as failure, no expression pasted into a script.
- install.sh's Termux refusal names the APT channel.

## Decisions for the lead
- W49-c-1 (canary spelling), W49-c-5 (placeholder digest warning), W49-c-7 (release lane
  wiring deferred), W49-c-8 (updater message) above.

Compare checklist (filled after the rewrite; reopened `scripts/termux/deb_version.py`,
`stage_apt_repo.py`, `termux_build.sh`, `build_deb.sh`, `termux-builder.Dockerfile`,
`check_apt.sh`):
- [x] Stable and canary mapping, `~` below stable (test: `tests/unit/release/test_termux_versions.py::test_version_table`, `::test_canary_sorts_between_stables`, `::test_our_ordering_agrees_with_dpkg`)
- [x] Malformed tags rejected; channel from the same parse (test: `test_termux_versions.py::test_malformed_tags_are_refused`, `::test_channel_follows_the_tag`)
- [x] Unsigned Release → exit 3, also when gpg is missing (test: `tests/unit/release/test_termux_repo.py::test_unsigned_release_exits_3_and_drops_stale_signatures`, `::test_unsigned_when_gpg_is_missing`)
- [x] Release has a Date and keeps its sums in one paragraph; Packages stanzas blank-line separated (test: `test_termux_repo.py::test_golden_repository_listing`, `::test_stable_and_canary_accumulate_in_order`)
- [x] Published versions immutable (test: `test_termux_repo.py::test_a_published_version_is_immutable`)
- [x] Isolated gpg home, passphrase on stdin, exactly one secret key, VALIDSIG for that key, public key at the root (test: `test_termux_repo.py::test_gpg_signer_keeps_the_passphrase_out_of_argv`, `::test_gpg_signer_refuses_several_keys_and_bad_signatures`, `::test_real_gpg_signs_and_verifies`)
- [x] Host must be aarch64, tools present, before any work (test: `tests/unit/release/test_termux_build_script.py::test_preflight_refuses_a_non_arm_host`, `::test_preflight_refuses_before_any_tool_runs`)
- [x] `.deb` payload under the on-device prefix, xz members, no Depends (self-contained), `.install_method` = `apt` (test: `test_termux_build_script.py::test_the_script_keeps_its_contract`)
- [x] Gap: native wheel builds need serial jobs, `ANDROID_API_LEVEL=24` and a `/bin/sh` for configure scripts — fixed in `build.sh` (wheels stage runs as root with a tmpfs `/bin`); unverified until the lane runs
- [x] Gap: the image's uid 1000 differs from the runner's — `build.sh` opens the two folders the container fills; unverified until the lane runs
- [x] Gap: the mirror's index can lag (retry `apt-get update` once) and cmake needs a newer `libc++` — fixed in `builder.Dockerfile`
- [x] Gap: duplicate fields in a control paragraph — refused (test: `test_termux_repo.py::test_unsafe_or_wrong_packages_are_refused[duplicate-fields]`)
- Deliberate differences: our tag grammar (W49-c-1); a `.deb` whose version differs from the
  tag's mapping is refused; zstd control members refused rather than decoded with an external
  binary (test: `[zstd]`); an identical rerun is a no-op instead of a refusal; the reference keys
  a recut by attempt ref in the pool path — we forbid any recut of a published version instead;
  the tag is read from the checkout (the lane checks it out from GitHub) rather than probed on
  origin; Termux's own runtime packages instead of lock-pinned source builds (W49-c-6).
- Not taken: SHA512 sums and `by-hash` indexes; commit-only builds; the wheelhouse cache and
  marker-aware PyPI probe (pip decides what to build); the R2 input mirror; the Windows gpg path
  form; the install/upgrade proof in a fresh container (`check_apt.sh`'s role) — follow-up as a
  `validate` stage in `build.sh` once docker can run.

## Integration (wave 49)

- W49-c-7 resolved at the integration (D-16.1143, D-16.1145): `termux` is an optional lane in
  `scripts/release/claims.py` (`OPTIONAL_LANES`, off unless `release.py --lanes` names it) and a
  `release.yml` job calling `termux.yml` with `tag` (admit's `v<version>`) and the new optional
  `commit` input (the admitted commit). Called that way, `termux.yml` checks the commit out and
  names it with the tag locally before `build.sh` (never pushed; a tag that already names another
  commit stops the build). Dispatched by hand it still checks out `refs/tags/<tag>`. Pinned by
  `tests/unit/ci/test_termux_workflow.py::test_release_lane_names_the_admitted_commit_with_the_tag`
  and `tests/unit/ci/test_release_workflow.py::test_termux_lane_calls_its_workflow_on_the_admitted_commit`.
  The lane has never run.

## Follow-up (wave 50, slice d)

- W49-c-8's message is built: `indus update` knows the `apt` stamp as its own install kind and
  refuses with `pkg upgrade indus-agent` (`docs/notes/followups/w50-d.md`, W50-d-1, W50-d-2).
- `build.sh` gained the `validate` stage between assembling and signing (stages `repo`, `validate`,
  `sign`; `repo.py validate` / `repo.py sign`, exit 4 for an invalid tree; W50-d-3, W50-d-4). It
  checks the built repository and the package layout on the host; the install / upgrade proof in a
  fresh Termux container (`check_apt.sh`'s role) is still owed to the first lane run.
