# Task: THIRD_PARTY_NOTICES generation (P25-17) Phase: 25 Owner: builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `LICENSE` (the copyright line only,
for the verbatim credit, D-16.40). Our own `docs/plan/research/10-research-packaging-ci.md` §8.1 and
the hand-written `THIRD_PARTY_NOTICES.md` (both ours, not reference files). Evidence: none.

What it does (own words): `scripts/release/notices.py` writes `THIRD_PARTY_NOTICES.md` from what we
actually ship: every package the Python lock reaches from the project's dependencies and extras,
every package the pnpm lock reaches from a workspace app's or package's production dependencies,
the binaries the installers download (`scripts/install/pins.json`: uv, the managed Python build,
portable Git for Windows) and what the Docker image adds (`docker/pins.json`: the Debian base
image and the bundles marked as shipped, s6-overlay and Node.js). Components are grouped by their
SPDX licence expression; the full text of every licence that occurs is included once at the end.
The credit to the reference (its MIT text with its copyright line, verbatim) opens the file.
`--check` regenerates in memory and fails when the committed file differs. Generation fails when a
component's licence is unknown, is not on the shared allow-list (`tooling/checks/licenses_allow.json`,
the same file the licence guard reads, including its per-package exceptions), when a copyleft
licence (MPL, LGPL, GPL, EPL, CDDL families) has no obligation note, or when a licence text is
missing.

Locks do not record licences, and most extras are not installed in CI, so licences come from a
committed table, `scripts/release/notices_data.json`, keyed by package and version. `refresh`
fills it from installed metadata (Python distributions, `node_modules`), keeping hand-checked rows
whose version did not change; a locked package whose version has no row is "unknown" until the
table is refreshed or the row is written by hand. The same file holds the licences of the pinned
binaries and the short notes for things that are not packages (test fixtures, build tools).

Inputs → outputs: `uv.lock`, `pnpm-lock.yaml`, `scripts/install/pins.json`, `docker/pins.json`,
`scripts/release/notices_data.json`, `scripts/release/licence_texts/*.txt`,
`tooling/checks/licenses_allow.json` → `THIRD_PARTY_NOTICES.md`.
`python scripts/release/notices.py [--check | refresh]`.

Files: `scripts/release/notices.py`, `scripts/release/notices_data.json`,
`scripts/release/licence_texts/`, `tests/unit/release/test_notices.py`, `THIRD_PARTY_NOTICES.md`
(now generated), `tooling/checks/licenses.py` + `licenses_allow.json` (obligation notes shared
with the generator), `docker/Dockerfile` and `apps/desktop/electron-builder.config.cjs` (embed the
file), `.github/workflows/ci.yml` (freshness step).

Edge cases / failure modes:
- the same package at two versions in a lock: both listed.
- a pnpm dependency on a workspace package (`link:`): our own code, skipped.
- peer suffixes in pnpm versions (`1.2.3(react@19.0.0)`): stripped for display, kept for the walk.
- a licence expression with `OR`: allowed when one side is allowed; texts of every id included.
- `WITH` exceptions only widen a licence (as the guard treats them).
- an extra added by P24 brings packages the table does not know: generation fails naming each one
  with "run `notices.py refresh` with the extras installed".
- a new key in `pins.json` (a new pinned binary) without a licence row: fails.
- docker bundles marked "never shipped" (CI tools) are left out.

Our design differences / decisions:
- Committed licence table instead of reading metadata at build time: the file must be identical on
  every machine and in CI, which has no extras installed.
- Licence texts are reproduced once per SPDX id (from the licence's canonical text), not once per
  package; each package's own copyright line stays in its distribution.
- Test fixtures and build-only tools are listed in short sections of their own (not shipped).

Tests we will write:
- a fixture lock with an unknown licence fails, naming the package; a not-allowed licence fails; an
  allowed one with a per-package exception passes; copyleft without an obligation note fails.
- stale file fails `--check`; the real file is fresh.
- the real file lists every runtime dependency from both locks and the pinned binaries.
- the reference credit line is present exactly once.
- the uv and pnpm walks: extras attribution, markers ignored, link deps skipped, peer suffixes.
- Docker image and desktop package embed the same file.

## Decisions for the lead
- `licenses_allow.json` gains per-package exceptions for components that ship today under a licence
  outside the allow-list while the owner's ruling is open (numpy's bundled Zlib parts, regex's
  CNRI-Python part, pyarrow's bundled Zlib/BSL-1.0, av's LGPL FFmpeg, Git for Windows GPL-2.0 with
  its source offer). Each reason is dated and points at the open owner item; remove an entry when
  the owner rules against shipping it.
- `obligations` (new key in `licenses_allow.json`) holds the copyleft notes printed in the file.
- Lead wiring after P24 lands: `uv sync --all-extras`, `python scripts/release/notices.py refresh`,
  check the new rows, then regenerate.

Compare checklist (filled after the rewrite; reopened the reference `LICENSE`):
- [x] credit line verbatim, exactly once (test: `tests/unit/release/test_notices.py::test_reference_credit_line_appears_exactly_once`)
- [x] Python, uv and Git for Windows redistributed by the installer carry notices with obligations (test: `tests/unit/release/test_notices.py::test_real_file_lists_every_runtime_dependency`)
- Deliberate differences: the reference ships no generated notices file; ours is generated and checked fresh.
- Not taken: per-package copyright lines (they stay in each distribution).

## Status

Done (2026-10-21). See the final report.

## Wave 46 (F-2): the desktop licences screen

Read: no reference file and no Evidence (our own screen over our own notices file).

What it does: Settings → About gains a second page, **Licences** (`view=about&page=licences`, also
a search hit for "licences", "licenses", "notices", "open source", "third party"), which shows the
`THIRD_PARTY_NOTICES.md` the packager already puts beside the app (`electron-builder.config.cjs`
`extraResources`, P25-17) as plain text in a scrolling, keyboard-reachable block.

- Main: `electron/diagnostics.ts` `readNotices(resourcesPath)` reads
  `<resourcesPath>/THIRD_PARTY_NOTICES.md` (null when not packaged, missing, not a regular file or
  over 4 MiB; the real file is about 140 KB), answered on the new invoke channel
  `indus:about:notices` (group `about`, method `notices`, owner `W46`; request `{}`, response
  `{ text: string | null }`). It goes through the registry like every handler (`check:ipc`), and
  `src/global.d.ts` is regenerated (`pnpm gen:bridge-types`).
- Renderer: `src/settings/LicencesPage.tsx` (custom page `about-licences`), the `about` bridge
  group on `SettingsBridge`. The text is rendered as a React text child inside `<pre>`, never as
  HTML or Markdown, so nothing in the file can inject markup. A development run (no
  `resourcesPath`) or an older main without the channel shows a one-line reason instead.

Tests: `electron/diagnostics.test.ts` (reader: null in dev, missing, oversized, a directory; the
handler answers null unpackaged; the builder config ships the file under the name main reads);
`src/settings/SettingsOverlay.test.tsx` (the About nav reaches the page and shows the text with
markup kept as text; no bridge / null text explains why; search "licenses" lands on the row).

### Decisions for the lead
- W46-d-3 (D-16.1002): the notices are read by main and sent over IPC, not fetched by the renderer from a
  file URL (the renderer has no file access). The channel lives with diagnostics, which already
  holds `resourcesPath`, so `main.ts` is unchanged.
- W46-d-4 (D-16.1003): plain text, not rendered Markdown: the file is long and generated; plain text is safe
  without a sanitiser and still readable. Rendering it with headings is a later polish.

## Wave 46 (builder g): refresh writes Biome's layout (release checklist RS7)

Read: none from the reference (our own generator; Biome's JSON layout checked against the
repository's pinned Biome binary).

- Gap: `refresh` wrote `notices_data.json` with `json.dumps(indent=2)`, one array element per line;
  Biome folds an array onto one line when it fits, so the lint gate failed after every refresh.
- Now: `dump_json` writes the layout Biome keeps: every non-empty object one key per line (Biome
  keeps an object expanded when its first key starts a new line), `{}` / `[]` inline, an array of
  scalars or arrays on one line when that line, its trailing comma counted, fits `lineWidth` 100
  (`biome.json`), else one element per line, each element judged again at its own indent. Width is
  counted in code points (Biome counts display width, so East Asian wide characters would differ;
  none occur in licence data).
- Checked: 160 generated cases around the width boundary (nested rows, empty containers, objects
  inside arrays, non-ASCII) came out byte-identical to `biome format`; the committed file
  round-trips unchanged.
- Tests: `tests/unit/release/test_notices.py::test_dump_json_*` (three; all failed on the old
  `dump_json`).
- Note: `scripts/release/*` is the wave-44 integrator's area; this slice changes only `dump_json` in
  `notices.py` (wave 44's patch does not touch that file).
