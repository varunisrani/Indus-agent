# Task: Bootstrap pins (P23-02) Phase: 23 Owner: wave-36 builder I1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/install.sh` (the generated
pinned-uv block and the target-key mapping), `scripts/install.ps1` (the generated pinned git/uv
block, ~80–112). Closed before writing.
Evidence: none.

What it does (own words): the installers never fetch an unpinned tool. One JSON file,
`scripts/install/pins.json`, names the uv version and, for every target the installers support
(linux x64/arm64 on glibc and musl, darwin x64/arm64, windows x64/arm64), the upstream URL, a
mirror URL on our own release assets and the SHA-256 of the archive; the same for portable Git for
Windows per arch; and the Python minor to provision (3.14, floor 3.13). It also holds the release
repository (owner/name) — the one constant every installer URL derives from (D-16.690 is open).
`pins.py render` turns the JSON into the shell section the built `install.sh` carries (and a
PowerShell section for the Windows build); `pins.py --check` fails when the JSON is malformed or the
section inside `install.sh` differs from what the JSON renders. `pins.py update` downloads the named
version for every target, computes the digests, rewrites the JSON and the built scripts, and prints
what to put up for review — it never commits or merges.

Inputs → outputs: `pins.json` → `render_sh()` / `render_ps1()` text; `--check [--strict]` → exit 0/1
with one line per problem; `update --uv X [--git Y]` → new JSON + rebuilt scripts.

Files: `scripts/install/pins.json`, `scripts/install/pins.py`, `tests/unit/packaging/test_pins.py`,
`.github/workflows/ci.yml` (shared: installer freshness + pins check step).

Edge cases / failure modes:
- a digest that is not 64 lowercase hex, a missing target, a non-https URL, a mirror outside our
  release base → check fails naming the field.
- the section in `install.sh` hand-edited (a tampered digest) → check fails.
- no network here: the digests in the first `pins.json` are not known. They are `null` with
  `"provisional": true`; the shell refuses to download a tool whose digest is unknown (never
  installs unverified bytes); `--check --strict` (release lanes) fails while any digest is null.
- update: a download failure aborts before anything is written.

Our design differences / decisions:
- No checked-in fragment file: the rendered section lives between markers inside `install.sh`;
  `build.py` writes it, `pins.py --check` compares it.
- Mirror assets live on one fixed GitHub release (`bootstrap-mirror`) of our repository, named
  `<tool>-<version>-<platform asset name>`; the digest is checked whatever the origin.
- The updater takes an injectable fetcher (tests feed bytes; the real one uses urllib).

Tests we will write:
- every target present; https only; mirror under our base; fragment equals JSON; tampered digest in
  the section fails; tampered JSON fails; strict mode fails on null digests; updater with a fake
  fetcher writes digests and refuses on a failed download; PowerShell render names every target.

## Decisions for the lead
- Release repository constant `indusagi/indus-agent` lives in `pins.json` → `release.repository`
  (D-16.690 open: change it there; the desktop default in `apps/desktop` is P20's and separate).
- Digests are provisional (`null`) until someone runs `uv run python scripts/install/pins.py update
  --uv 0.11.32 --git 2.51.0.windows.1` with network; pinned versions are recorded in
  `tool-versions.md` and must be confirmed there.

Compare checklist (filled after the rewrite; reopened the two pinned blocks):
- [x] every target the installers support, URL + mirror + SHA-256 each (test: `tests/unit/packaging/test_pins.py::test_every_target_is_present`)
- [x] generated section in the built script, stale or hand-edited → check fails (tests: `::test_the_fragment_equals_the_json`, `::test_a_tampered_digest_in_the_script_fails_the_check`, `::test_a_tampered_digest_in_the_json_fails_the_check`)
- [x] PowerShell pins for uv and portable Git per arch (at integration `install.ps1` embeds `pins.json` itself: `tests/integration/install/test_install_ps1.py::test_embedded_tables_equal_their_json_files`)
- [x] gap found: the reference's generated block is the only record of the version string the
  install uses; ours also renders the Python range and repository slug from the same JSON
  (`render_sh`), pinned by `::test_the_fragment_equals_the_json`
- Deliberate differences: the reference derives its pins from its package manager's lock and
  mirrors by digest-addressed URLs on its own CDN; we keep a standalone `pins.json` and mirror on a
  fixed GitHub release of our repository (D-16.58). Digests start `null` (provisional) because this
  build had no network; the installer refuses rather than downloading unverified bytes.
- Not taken: a content-addressed mirror path (GitHub release assets are named, not addressed).

## Status

Done (2026-10-21) with provisional digests (see Decisions). Shared file changed:
`.github/workflows/ci.yml` (one step: `build.py --check` and `pins.py --check`).

## Integration (wave 36)

`pins.py update --uv 0.11.32 --git 2.51.0.windows.1` ran at integration: every digest recorded, `provisional` false, `--check --strict` passes (uv digests also match upstream's `.sha256` files; both PortableGit assets exist). `release.repository` is now a copy of `RELEASE_REPOSITORY` (`src/indus/foundation/versions.py`): `update` writes the constant and `--check` fails when they differ (D-16.740). `render --ps1` is gone: `install.ps1` embeds `pins.json` itself (`build.py`).
