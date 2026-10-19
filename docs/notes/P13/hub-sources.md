# Task: Skills hub sources (P13-25) Phase: 13 Owner: wave-2 builder N4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skills_hub_sources.py`
(well-known and URL adapters; LobeHub/browse.sh skimmed only to confirm they stay out),
`tools/skills_hub_github.py` (taps, tree listing, raw-content fetch, auth chain, rate limits),
`tools/skills_hub_skillssh.py` (search API, slug resolution, `skills.sh.json` groupings),
`tools/skills_hub_search.py` (fan-out, index-miss fallback budget), `tools/skills_hub_official.py`
(skimmed: optional catalogue source), `tools/skills_hub_models.py` (record shapes, path checks,
referenced-file extraction). Closed before writing.
Research: `docs/plan/research/07-learning-skills-plugins-cron.md` §3.10, §3.12.

What it does (own words): `indus.learning.skills.hub.sources` holds one adapter per place a skill
can come from, all behind one async interface: `search(query, limit)`, `resolve(identifier)` (meta
without downloading the bundle) and `fetch(identifier)` (a `Bundle`: name, files, source,
identifier, trust). Every HTTP request goes through `HubHttp`, which wraps
`safety.safe_transport.safe_client` (P05 SSRF policy at connect time, each redirect hop
re-checked) with explicit timeouts; a refused or failed request is `None`, never an exception into
the caller.

Adapters:
- `official` — folders under the repo's optional catalogue dir (`optional-skills/` next to the
  shipped `skills/`, or `INDUS_OFFICIAL_SKILLS`); trust `builtin`; empty in 1.0 (F30). Identifier
  `official/<rel path>`. No network.
- `url` — a direct `https://…/SKILL.md` (or any `.md`) URL; always `community`. Name ladder:
  frontmatter `name` if it matches `^[a-z][a-z0-9_-]*$` → URL parent slug (or the file stem) if it
  matches → `awaiting_name` (the pipeline prompts on a TTY, else errors pointing at `--name`).
- `github` — `owner/repo/path/to/skill`; taps are repos with skills under a root (default
  `skills/`). Listing reads the Git tree once (`GET /repos/{r}` + `GET …/git/trees/{branch}`), so an
  install is about two API calls; files come from `raw.githubusercontent.com` pinned to the tree
  sha (`Authorization: Bearer` only with a token). Dirs starting with `.` or `_` are ignored. A private repo needs a token (`GITHUB_TOKEN` /
  `GH_TOKEN` from the scope's secrets/env); without one a 404 on the repo reads "not found, or
  private (set GITHUB_TOKEN)".
- `well-known` — `https://<site>/.well-known/skills/index.json` (`{"skills": [{name,
  description, files}]}`); identifier `well-known:<base>/<name>`.
- `skills-sh` — the skills.sh search API (`/api/search?q=`); identifiers `skills-sh/owner/repo/
  skill` resolve to GitHub (`<path>`, `skills/<path>`, `.agents/skills/<path>`, `.claude/skills/
  <path>`, then a tree search for `<skill>/SKILL.md`); a repo-root `skills.sh.json` maps skills to
  category labels.

Taps: `<home>/skills/.hub/taps.json` `{"taps": [{repo, path, trust}]}`; new taps are `community`;
`trusted` only for repos in the in-code `TRUSTED_TAPS` set (empty). Search: every configured
source within the remaining budget (default 30 s); when the configured sources found nothing for a
non-empty query, a live fallback asks skills.sh and the well-known sites of
`skills.hub.well_known`, capped at 8 s; a source or tap filter never triggers it. "Configured
sources" are the official catalogue and the taps.

Inputs/outputs: scope (secrets, config, home), optional `HubHttp`. Output `SkillMeta` rows and
`Bundle`s. Only files `SKILL.md` references under `references/ templates/ scripts/ assets/
examples/` are fetched (plus nothing else from a repo).

Edge cases: traversal or absolute paths in an index or a reference → bundle refused; symlink
entries in a GitHub tree referenced by `SKILL.md` → refused; a truncated tree → fall back to the
contents listing; 403/429 rate limit → clear error with the token hint; non-UTF-8 `SKILL.md` →
refused.

Our differences: no ClawHub/LobeHub/browse.sh (F27, P3), no central index (we do not run one);
GitHub fetch copies only referenced files (the reference now copies the whole skill dir); trust
set empty by default; async adapters on one SSRF-safe client.

Tests planned (`tests/unit/learning/skills/hub/test_sources.py`, MockTransport behind
`SafeTransport` with a fixed resolver): URL name ladder incl. non-TTY error; GitHub tap lists
subdirs with `SKILL.md`, ignores `.`/`_`; well-known index parsed and fetched; skills.sh search +
groupings; search fallback stops at 8 s (fake clock); private repo without token → clear error;
official empty catalogue works (ACC-F30-1); traversal in an index refused.

## Decisions for the lead
- Optional catalogue dir: `INDUS_OFFICIAL_SKILLS`, the packaged `indus/_official_skills`, else the
  checkout's `optional-skills/` (absent today; no packaging rule added until it has content).
- `TRUSTED_TAPS` is an empty frozenset in `hub/sources.py`; the owner fills it. A tap is
  `trusted` only when it is in that set and `indus skills trust <repo>` marked it.
- No default taps ship (the reference ships several third-party repos); `browse` shows the
  catalogue and the user's taps only.
- skills.sh: `GET /api/search?q=&limit=` answering `{"skills": [{id | source+skillId, name,
  installs}]}` is taken from the reference's behaviour — not checked against published docs (no
  network here). Follow-up: verify before 1.0.
- Search budget `skills.hub.search_timeout` (30 s); fallback cap fixed at 8 s.
- Bodies over 1 MiB are refused per file.

Compare checklist (after writing; reopened the files above):
- [x] URL name ladder and reserved stems (`skill`, `readme`, `index`) — same intent, own ladder.
- [x] Tap dirs starting with `.`/`_` skipped; tree pinned by sha; raw host first.
- [x] Fallback budget 8 s only when configured sources found nothing and no filter is set.
- [x] Referenced-file extraction fails closed on `..`; intended difference: no whole-dir copy.
