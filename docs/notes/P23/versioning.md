# Task: Versioning and channel records (P23-21) Phase: 23 Owner: wave-36 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/releases/versioning.py`
(all), `website/docs/developer-guide/stable-releases.md` (order and versioning sections). Research 10
§3.17, traps R10.26, R10.27. Closed before writing. Evidence: none.

What it does (own words): one module owns the version grammar and every caller (the updater, the
release command, the publication controller, the workflows' Python steps) imports it. The source
tree says `0.0.0`; a stable release is plain SemVer `X.Y.Z` (0.x during the build, 1.0.0 at
release); a canary is `X.Y.Z-canary.<YYYYMMDDTHHMMSSZ>` where `X.Y.Z` is the patch after the newest
stable, so plain SemVer precedence puts every canary above the last stable and below the stable it
leads to — the order needs no side table. The channel words are exactly `stable`, `canary`, `main`.
The next stable comes from the newest *published* (not draft, not prerelease) GitHub release, bumped
by patch/minor/major. Attempt refs `rc.<N>-vX.Y.Z` and their abandon markers are parsed by the same
module. Channel records (latest stable and canary: version, tag, commit, image digests) are one JSON
asset `channels.json` on a fixed release tagged `channels`; only the publication controller writes
it, and only after comparing the asset's current SHA-256 with the one it read (an If-Match check);
a record never moves to an older version.

Inputs → outputs: tag/version strings, published release rows, a clock → `Version`, next versions,
attempt numbers, channel-record JSON; `RecordConflict` when the asset changed underneath.

Files: `src/indus/foundation/versions.py` (grammar; product code can import it, scripts cannot be
imported by product code), `scripts/release/versioning.py`, `tests/unit/release/test_versioning.py`.

Edge cases / failure modes:
- malformed tags (`v1.2`, `1.2.3`, `v1.2.3-rc.1`, `v1.2.3+canary.x`, `v01.2.3`, `v1000.0.0`,
  `v1.2.3-canary.20261341T000000Z`) parse to nothing;
- no published release → next stable is the bump of `0.0.0`; next canary `0.0.1-canary.<stamp>`;
- a draft or prerelease row never counts as published;
- the channel asset missing → first write expects "no asset" as its previous state;
- two writers: the second sees a different digest and is refused.

Our design differences / decisions:
- canaries are SemVer prereleases of the *next* patch (`-canary.`), not build metadata of the
  current stable (`+canary.`), so ordering comes from SemVer itself (R10.26);
- no legacy CalVer seed (D-16.3);
- channel records live on GitHub Releases (D-16.58) instead of object storage; the compare-and-swap
  is done by digest because GitHub release assets have no ETag precondition;
- the release repository is one constant, `RELEASE_REPOSITORY` in `indus.foundation.versions`
  (owner decision D-16.690 pending).

Tests we will write: table tests for next versions; canary ordering over many seeded random stamps
(hypothesis is not a dev dependency; a seeded property loop instead); malformed tags rejected; grep
test that the grammar is defined once and imported by every caller; record update refused when the
previous digest changed; no backwards record move.

## Decisions for the lead
- `RELEASE_REPOSITORY = "indusagi/indus-agent"` (the desktop's D-16.690 guess) until the owner
  decides; installers and Docker slices should read the same constant.

Compare checklist (filled after the rewrite; reopened `scripts/releases/versioning.py` and the doc):
- [x] attempt grammar puts the attempt before the version (`rc.<N>-vX.Y.Z`)
  (test: `tests/unit/release/test_versioning.py::test_attempt_and_marker_refs_round_trip`)
- [x] attempts do not spend versions; markers never free an attempt number
  (`::test_next_attempt_counts_markers_as_spent`)
- [x] next version from the newest published non-prerelease (`::test_next_versions_table`)
- [x] canary below its own stable and above the last (`::test_canary_ordering_property`)
- [x] one grammar (`::test_grammar_is_defined_once_and_imported_by_every_caller`)
- [x] record compare-and-swap (`::test_record_update_refused_when_the_previous_digest_changed`)
- Deliberate differences: canary as prerelease of the next patch (above); signed-tag armour is
  stripped by the shared tag-record reader as well.
- Not taken: the protected object-storage channel heads.

## Status

Done (2026-10-21). New shared module `src/indus/foundation/versions.py`.

## Integration (wave 36)

Integration: `SANDBOX_IMAGE = RELEASE_IMAGE + "-sandbox"` joins the constants; `RELEASE_REPOSITORY` is the one release base every other file copies or is tested against (D-16.740).
