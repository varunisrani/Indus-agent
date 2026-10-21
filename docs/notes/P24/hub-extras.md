# Task: More hub sources, skill publish and snapshots (P24-45) Phase: 24 Owner: wave-38 builder S1; finished by wave-39 builder B4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skills_hub_clawhub.py` (all),
`tools/skills_hub_sources.py` (the LobeHub and browse.sh adapters only), `hermes_cli/skills_hub.py`
(`do_publish` + its GitHub helper, `do_snapshot_export`, `do_snapshot_import` only). Closed before
writing; reopened by B4 (wave 39) for Compare. Research: `docs/plan/research/07-learning-skills-plugins-cron.md` §3.10. Evidence: none.

Vendor API pins (from reference behaviour; to re-check against the vendors' docs before 1.0):
ClawHub HTTP API v1 (`https://clawhub.ai/api/v1`: `GET /skills?search=&limit=`, `GET /skills/<slug>`
(`?owner=`), `GET /skills/<slug>/versions`, `GET /skills/<slug>/versions/<v>`, `GET /download?slug=
&version=` → zip); LobeHub agent index (`https://chat-agents.lobehub.com/index.json`,
`/<id>.json`); browse.sh (`https://browse.sh/api/skills`, `/api/skills/<host>/<task>` →
`skillMdUrl`); GitHub REST `X-GitHub-Api-Version: 2022-11-28`.

What it does (own words):
- Three more hub sources behind P13's `Source` interface, all `community` trust, every bundle
  going through the normal quarantine → scan → trust policy (R07.26):
  - `clawhub/<slug>` or `clawhub/@owner/<slug>`: search by the listing API; detail by slug (owner
    must match when given); content = the latest version's zip (≤ 25 MiB download, members checked
    with the hub's safe-path rule, only `SKILL.md` and support-folder files kept, ≤ 1 MiB each),
    falling back to the version record's inline files.
  - `lobehub/<agent>`: agent entries of the index; fetching converts an agent (title, description,
    tags, system role) into a `SKILL.md` of our own layout. Its system role is untrusted text: a
    role carrying an injection marker is blocked by the scanner like any skill.
  - `browse-sh/<host>/<task>`: site-automation skills from the catalogue; content always through
    the per-skill detail endpoint's `skillMdUrl` (http/https only).
  The live-search fallback (no hits in the configured sources, a real query, no filter) now asks
  skills.sh, ClawHub, LobeHub, browse.sh and the well-known sites together within the existing
  8 s cap.
- `indus skills publish <skill> --to github --repo owner/name [--path skills] [--branch B]
  [--no-pr]`: checks the skill (SKILL.md with name + description, scan not `dangerous`, no file
  link escaping the folder — refused before any request), then with the user's own GitHub token
  (`GITHUB_TOKEN`/`GH_TOKEN` from the secret store or env) creates one commit holding exactly the
  skill folder's files under `<path>/<name>/` on a new branch — in the repo itself when the token
  can push there, else in the user's fork (created when missing) — and opens a pull request unless
  `--no-pr`. The token is a header only.
- `indus skills snapshot export [-o FILE|-]` writes `{format, version, exported_at, skills: [{name,
  source, identifier, category, content_hash, upstream_hash}], taps}`; `snapshot import FILE
  [--force] [-y]` restores taps and reinstalls each skill through the normal install path; a
  skill whose upstream content now scans `dangerous` is refused even with `--force`; one whose
  content differs from the snapshot's hash is installed with a note. Exit 1 when any failed.

Files: `src/indus/learning/skills/hub/sources_extra.py`, `.../hub/publish.py`,
`.../hub/snapshot.py`, tests `tests/unit/learning/skills/hub/test_sources_extra.py`,
`test_publish.py`, `test_snapshot.py`; shared: `.../hub/sources.py` (SourceSet gains the three
sources; fallback list; identifier routing before GitHub's), `cli/commands/skills_hub.py`
(`publish`, `snapshot export|import`), CLI contract tables.

Edge cases / failure modes:
- ClawHub 409 for a slug shared by owners → the `@owner/` form is required (we pass `owner`).
- zip with `../` members, absolute paths or links → those members skipped; no `SKILL.md` → None.
- LobeHub agent ids outside `[A-Za-z0-9._-]` refused; missing system role → a placeholder line.
- publish: repo not `owner/name`, no token, 403/404 from GitHub → one clear message, nothing
  partial except a branch in the user's own fork (named in the message).
- snapshot: unreadable JSON, entries without identifier (skipped with a note), unknown format.

Our design differences / decisions:
- one atomic commit through the Git data API (blobs → tree → commit → ref) instead of one
  contents call per file; same-repo branch when the token can push.
- no ClawHub publishing target (only `github`), no catalogue walking/caching for ClawHub, no owner
  enrichment; LobeHub and browse.sh catalogues read once per hub run.
- snapshot records content hashes and reports drift (reference only lists identifiers).

Tests we will write:
- respx fakes per source: search, resolve, fetch (zip + fallback, owner mismatch, unsafe zip
  member, browse.sh detail URL), LobeHub conversion; an injection-marked LobeHub entry blocked by
  the install pipeline; fallback search reaches the new sources.
- publish: symlink escaping the dir refused (no request made), dangerous refused, PR flow on a
  respx GitHub (fork when no push rights; only skill files in the tree; token never in URLs).
- snapshot: export shape; import reinstalls; now-dangerous refused even with `--force`.

## Decisions for the lead
- `publish` default opens a PR; `--no-pr` only pushes the branch.
- snapshot format id `indus-skills-snapshot` v1.

Compare checklist (B4, wave 39: reopened the three reference files):
- [x] ClawHub identifier forms and owner check; zip size bound — owner checked on the detail and
  (added after Compare) on the version record before its inline files are used; 25 MiB download
  bound (declared and streamed); unsafe members skipped. Ours accepts only the `clawhub/` forms
  (bare slugs and `owner/skills/slug` URL paths are not routed to ClawHub); members ≤ 1 MiB
  (reference 500 KB); no 429 retry with Retry-After; inline `rawUrl` files are not fetched
  (another host per file).
- [x] LobeHub conversion fields (identifier, title, description ≤ 500, tags, system role,
  placeholder when none); browse.sh detail endpoint `skillMdUrl` (http/https). Not taken: the
  reference's fallback to a `raw.githubusercontent.com` `sourceUrl` when the detail has no link
  (the row says content comes through the detail endpoint).
- [x] publish checks (description required, dangerous refused); snapshot taps restored — an entry
  without identifier is skipped, not a failure (aligned after Compare); already-installed entries
  are kept (reference reinstalls); snapshot records hashes and reports drift (ours).
- Deliberate differences: see above.
- Not taken: see above; ClawHub publishing target (reference prints "not yet supported").

## Status

Done (2026-10-21, wave 39 B4): snapshot tests `tests/unit/learning/skills/hub/test_snapshot.py`
(export shape, reinstall through the pipeline with taps restored, kept when installed, a
now-dangerous skill refused even with `--force`, caution needs `--force`, drift note, bad
snapshots, CLI exit codes); Compare walked; trap R07.26 pinned to the LobeHub injection test and
the snapshot `--force` test. Vendor API shapes (ClawHub, LobeHub, browse.sh) still to be checked
against the vendors' docs before 1.0 (no network here). ACC-F27-1 has no row in
`docs/internals/acceptance-map.md` (the map lists P0/P1 features only).
