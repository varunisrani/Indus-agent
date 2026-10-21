# Task: Clean-room sweep of built artefacts (P25-19) Phase: 25 Owner: wave-41 builder G1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — our own guards (banned
identifiers, asset hashes) applied to build outputs. Research R09.22, R10.29 (our specs).
Evidence: none.

What it does (own words): `python -m tooling.checks artifacts --artifact PATH …` (or
`INDUS_ARTIFACTS`, paths joined by the OS path separator) opens each built output and checks
every member — through folders (in full: bundles ship `node_modules`), ASAR archives (our own
reader of the pickle header + JSON index; unpacked files are on disk beside it), tar archives
(`docker save` output: manifest, image config with its labels, every layer recursed; a top-level
image tar is streamed) and zip archives (`.zip`, `.msix`, `.appx`, `.nupkg`, any `PK` file):

- member paths and text members against `banned_patterns.json` (reference names, vendor hosts,
  brand fonts);
- media members against the reference's media hashes (`assets`), when the checkout is present
  (`--require-ref` makes its absence a setup error);
- text members against the building machine's home folder (R10.29: an absolute build path in a
  source map or bundle is personal data).

`tooling/checks/artifacts_allow.json` lists member globs that may match, each with a reason
(empty). No artefact named → a note, exit 0 (the everyday run); a named path that does not exist
→ setup error.

Files: `tooling/checks/artifacts.py`, `tooling/checks/artifacts_allow.json`,
`tooling/checks/tests/test_artifacts.py`, `tooling/checks/__init__.py` (registered),
`tooling/checks/__main__.py` (`--artifact`), `tooling/checks/common.py` (`Context.artifacts`).

Tests: ASAR reader round trip and refusal; a clean unpacked release passes; a seeded banned name
inside a fake ASAR fails with `app.asar!dist/index.js:1:17`; a banned name in a member path; a
vendor host in the web dist; an image tar swept layer by layer; image labels; zip installers; a
reference asset inside an ASAR; the build machine's home folder; the allow-list (and its reason);
nothing named / missing artefact; artefacts from the environment.

Real run here: web dist (fresh `vite build` into scratch) + `scripts/install.sh`, `install.ps1`,
`install.cmd`: 38 members, 0 issues, 210 reference media hashed. Not run here: desktop ASAR and
Docker image (no Electron package or Docker in this container), docs site (P25-14, slice D1).

## Decisions for the lead
- CI wiring (shared `ci.yml`, left to the lead): after the web build, the docs build, the desktop
  `--dir` package and `docker save`, run
  `uv run python -m tooling.checks artifacts --require-ref --artifact apps/web/dist
  --artifact site --artifact apps/desktop/build/release/linux-unpacked --artifact image.tar
  --artifact scripts/install.sh --artifact scripts/install.ps1 --artifact scripts/install.cmd`.
- Text members over 32 MB are path- and hash-checked only.

## Status

Done (2026-10-21).
