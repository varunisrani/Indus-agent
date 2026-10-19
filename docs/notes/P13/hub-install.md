# Task: Hub install pipeline, lock and lifecycle (P13-26) Phase: 13 Owner: wave-2 builder N4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skills_hub_install.py`
(quarantine, target safety, install, uninstall, update check), `tools/skills_hub.py` (lock file,
taps, audit log, state-file locking), `tools/skills_hub_models.py` (path validators). Closed before
writing. Research: `docs/plan/research/07-learning-skills-plugins-cron.md` §3.10.

What it does (own words): `indus.learning.skills.hub.install.install(...)` runs one pipeline:
1. resolve the identifier to an adapter (`--category`, `--name` honoured);
2. fetch into `<home>/skills/.hub/quarantine/<id>/` — `SKILL.md` plus only referenced support files;
3. format check (`check_skill_text`; an unparsable skill is refused) and lint (advisory), then
   `content_scan.scan_tree`;
4. policy by trust × verdict: builtin allow/allow/allow, trusted allow/allow/block, community
   allow/block/block; `force` lifts a `caution` block only, never `dangerous`;
5. target safety: no symlink/junction on the way, never onto a category bucket or inside another
   skill, no symlink inside the bundle; then move into `<home>/skills/[category/]<name>/`;
6. lock entry in `<home>/skills/.hub/lock.json` and one line in `.hub/audit.log`;
7. open sessions see the new skill through the P09 index delta note (nothing extra to do: the
   catalog rescans).
Lock file (P13 owns the shape; P09 readers updated): `{"version": 1, "installed": {name: {name,
source, identifier, install_path, content_hash, upstream_hash, trust, verdict, scanner_version,
findings, files, ts}}}`. `content_hash` is the installed tree, `upstream_hash` the bundle as
fetched (they differ only when the pipeline rewrote the frontmatter `name`). One hash function for
trees and bundles (sorted posix paths + bytes).
Lifecycle: `check` refetches each entry from its recorded source and compares bundle hashes
(orphaned and unsafe entries reported without network, never removed); `update` reinstalls only
changed skills and skips locally edited ones (disk hash ≠ `content_hash`) unless `force`; `audit`
re-scans every hub skill; `uninstall` removes the dir named by the lock entry (validated) and the
entry. Hub skills: no usage telemetry writes, never curator-managed (P09 readers use the lock).
Advisory second scanner: `skills.hub.advisory_scanner` names a binary; when it is on PATH its
output is printed, never blocking.

Edge cases: name collision with a non-hub skill → refused unless `force`; reinstall over a hub
skill needs `force`; a `name:` that differs from the install name is rewritten in quarantine;
interrupted install leaves only a quarantine dir (cleared on the next run).

Our differences: lock key `trust` (P09 assumed it) instead of `trust_level`; `upstream_hash`
recorded; scanner is our `content_scan`; no metrics.

Tests planned (`tests/unit/learning/skills/hub/test_install.py`; downloads from a local HTTP
server with `security.allow_private_urls`): community caution blocked, `force` installs; dangerous
never installs; only referenced files copied; symlink target refused; lock + audit written; update
skips a locally edited skill; uninstall removes dir and entry; new skill visible as a delta note in
an open session.

## Decisions for the lead
- Lock shape above (adds `upstream_hash`, `verdict`, `files`). P09 readers now go through
  `hub.lock`: `usage.hub_names` (telemetry, curator, journey) and `invoke.is_community_skill`
  (inline shell). A lock row without a known trust counts as `community`. P09 test helpers write
  real entries (`tests/unit/learning/skills/helpers.record_hub`).
- A skill that fails `check_skill_text` is refused (it would not load anyway); lint is advice.
- When the install name differs from the frontmatter `name` (`--name`, URL slug, prompt), the
  quarantined `SKILL.md` gets that name so the resolver loads it under it.
- The hub never overwrites a skill it did not install (same path or same name in the profile's
  own folder): refused with "use another --name"; reinstalling a hub skill needs `--force`.
- `update --force` only means "overwrite my edits"; it never lifts a caution block (run
  `install --force` for that).
- Advisory scanner: `skills.hub.advisory_scanner` (empty = off), run as `<bin> <dir>`, 60 s,
  child env from `safety.child_env`.
- Follow-up for the scanner owner: a destructive command inside an inline code span
  (`` `chmod -R 777 /etc` ``) scans `safe`; the same text unquoted scans `caution`.

Compare checklist (after writing; reopened the files above):
- [x] Policy matrix and `--force` only for caution.
- [x] Lock install_path validated before any rmtree; skills root itself never removable.
- [x] Category bucket / nested skill / symlink refusals.
- [x] Update skips local edits unless forced; check never auto-removes.
