// Where Indus Agent releases live (D-16.740): the desktop's one copy of `RELEASE_REPOSITORY` from
// src/indus/foundation/versions.py. tests/unit/packaging/test_release_base.py holds the two equal
// and checks that the in-app installer and the update feed build their URLs from this constant.
// The value is an owner decision still open (D-16.690); change it in versions.py and here.

export const RELEASE_REPOSITORY = "indusagi/indus-agent";

/** Release assets: `<base>/v<version>/<asset>` (the in-app installer's scripts, P20-09). */
export const RELEASE_DOWNLOAD_BASE = `https://github.com/${RELEASE_REPOSITORY}/releases/download`;

/** The releases API the update check asks (P20-37). */
export const RELEASES_API = `https://api.github.com/repos/${RELEASE_REPOSITORY}/releases`;
