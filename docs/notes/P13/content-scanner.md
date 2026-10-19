# Task: Shared content scanner (P13-20) Phase: 13 Owner: wave-10 builder M3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skills_guard.py` (the
finding/result shapes, install policy table, verdict rule, the structural checks, the ignore-file
loader, the comment/denylist demotions, the markdown fence and link-destination masking, the
community "doc note" step-down, the scan cache; the regex corpus `THREAT_PATTERNS` was skipped —
only its category/severity tally was looked at). Closed before writing.
Research: `docs/plan/research/07-learning-skills-plugins-cron.md` §3.10 (install flow step 3),
§3.13 (plugin static scan, context scoring). Evidence: none.

What it does (own words): one static scanner, `indus.safety.content_scan`, that every path which
brings outside text or code into reach of the agent calls before activation: hub skills (P13-26),
project skills (P13-28), plugins (P13-21), optionally agent-created skills (P09 `skill_manage`) and
MCP tool descriptions. It walks a folder (or takes one text) and reads files as text only — it never
imports, executes or follows anything. Each match is a `Finding` (rule id, category, severity,
file, line, short excerpt). A finding in a place the agent does not act on — documentation prose
(README, `docs/`), whole-line comments in code, test trees and fixtures, changelogs, base64 media
inside a `data:` URI — is stepped down one severity and tagged with why ("context"); it stays in
the report. Agent-facing shapes (`SKILL.md`, scripts and other code, references the skill loads,
MCP descriptions) keep full severity. Verdict: any critical → `dangerous`, any high → `caution`,
else `safe`. Scanner id `indus-scan-v1` is stamped on every report so callers can cache by
(content hash, scanner id).

Finding categories (ours): `exfiltration` (secret files or secret env vars together with a network
sender, secret-file reads, env dumps, drop-site hosts), `injection` (prompt-injection phrases via
the landed `safety.threat_scan` patterns, chat-template tokens, approval-bypass talk),
`destructive` (wiping `/` or home, raw disk writes, fork bomb, recursive chmod of `/`, …),
`supply_chain` (download piped to a shell or interpreter, decode-then-execute, remote package
sources, long base64/hex blobs, shipped native binaries), `invisible_unicode` (TAG block, bidi
controls, zero-width runs), `traversal` (`..` in a path `SKILL.md` refers to, deep `../../..` in
code, symlinks leaving the root), `agent_config` (writing `AGENTS.md`, `CLAUDE.md`, other agents'
rule files, Indus's own config, persona, memory or skills dirs), plus `persistence` (SSH key
planting, shell-profile and cron/systemd hooks, sudoers) and `structure` (oversized or truncated
files, too many files).

Inputs → outputs: `scan_tree(root, kind=…, allowed_env=…)` / `scan_text(text, path=…, kind=…)` →
`ScanReport{scanner, target, kind, verdict, findings, files, ignored}`; `render_report(report)` →
text for CLI/chat; `verdict_of(findings)`.

Files: `src/indus/safety/content_scan.py`, `tests/unit/safety/test_content_scan.py`,
`tests/fixtures/content_scan/corpus.yaml`. Shared: `src/indus/safety/threat_scan.py` gains
`locate()` (the same patterns, but every match with its line) so the injection phrases are not
duplicated.

Edge cases / failure modes:
- `.skillignore` (gitignore basics: comments, blank lines, `dir/`, leading `/` anchors, `*`/`?`
  globs on the path and on each segment); `SKILL.md` and the ignore file itself can never be
  ignored / are never scanned respectively; ignored paths are listed in the report;
- `.git/` is never walked; symlinks are never followed: one that resolves outside the root is a
  critical traversal finding, a broken one is medium;
- binary files (NUL in the first 8 KiB or not UTF-8) are not read as text; native executables or
  libraries (`.exe`, `.dll`, `.so`, `.dylib`, …) are a high supply-chain finding;
- a file over 1 MiB is scanned only up to 1 MiB with a medium `structure` finding saying so;
  more than 2,000 files → the walk stops with a high finding (caution, never a silent pass);
- unreadable file → medium `structure` finding, never a crash;
- line numbers count `\n` only (no `str.splitlines`, which also splits on U+2028 and friends);
- a UTF-8 BOM at the start of a file is not an invisible-character finding; one mid-file is;
- Unicode subdivision flags (U+1F3F4 + TAG letters + U+E007F) are emoji, not smuggled text;
- a lone zero-width joiner/non-joiner (emoji sequences, Indic scripts) is not reported; a run of
  two or more zero-width characters is;
- a plugin that declares the env var it needs (`allowed_env`) may send that one to its API
  without an exfiltration finding (plugin-aware exemption).

Our design differences / decisions:
- scanner id `indus-scan-v1`; our own category names, rule ids and pattern corpus (written fresh;
  the reference's regexes were not read);
- one step-down rule for every non-agent-facing context (docs, comments, tests, changelogs, data
  URI media) instead of per-pattern demotions; at most one step;
- injection phrases reuse `threat_scan` (strict set) through the new `locate()`;
- curl-pipe-shell is `high` (caution, forceable), not critical: install one-liners are common in
  real skills; decode-then-execute is critical;
- the install policy matrix (trust × verdict) is left to the callers (P13-21, P13-26), which own
  it; this module only produces the verdict;
- no scan cache here (P13-26/P13-28 cache by content hash outside the repo).

Tests we will write:
- corpus: every positive flagged in its category (and at its severity when given), every negative
  clean;
- same command in `SKILL.md` vs a fenced block in `docs/guide.md` → critical vs high;
- TAG char in `SKILL.md` → dangerous; subdivision flag → clean;
- `../` in a link of `SKILL.md` → dangerous; same link in `README.md` → clean;
- comment, test tree, changelog and data-URI step-downs keep the finding with a context tag;
- `.skillignore` honoured, `SKILL.md` never ignored; symlink escaping the root; binary file;
  oversized file; file-count cap;
- sentinel: scanning a tree whose `__init__.py`, `conftest.py`, `setup.py` and scripts would write
  a file when run/imported leaves no file behind, and `sys.modules` gains nothing;
- `allowed_env` exemption; `render_report` ordering; `scan_text` for an MCP description.

## Decisions for the lead
- Extra categories `persistence` and `structure` beyond the row's list (the row's list is about
  threats; these two carry SSH/shell-profile planting from `threat_scan`'s strict set and the
  file-walk limits).
- `kind` (`skill`, `plugin`, `description`) is recorded on the report and switches the
  `SKILL.md`-only traversal rule; it does not change severities.
- Medium findings do not raise the verdict (safe with notes), as in the reference.

Compare checklist (filled after the rewrite; reopened `tools/skills_guard.py`):
- [x] verdict rule critical → dangerous, high → caution, medium/low → safe (test: `test_content_scan.py::test_verdict_rule`) — matches `_determine_verdict`.
- [x] ignore file semantics incl. SKILL.md never ignored (test: `test_skillignore_excludes_but_never_skill_md`).
- [x] structural checks: symlink escape, native binary, oversized, file count (tests: `test_symlink_outside_root_is_dangerous`, `test_native_binary_is_supply_chain`, `test_oversized_file_is_truncated_and_noted`, `test_file_count_cap`).
- [x] comment step-down; doc step-down; test/changelog step-down (tests: `test_comment_step_down`, `test_docs_code_block_one_level_lower`, `test_test_tree_and_changelog_step_down`).
- [x] invisible characters per line; subdivision flags kept (tests: `test_tag_character_in_skill_md_is_dangerous`, `test_subdivision_flag_is_clean`, `test_zero_width_run_vs_single_joiner`).
- [x] markdown link destinations masked in prose only (tests: `test_parent_link_in_skill_md_is_dangerous`, `test_parent_link_in_readme_is_clean`).
- [x] never imports/runs a scanned file (test: `test_scan_never_imports_or_runs_a_file`); `.git` not walked (`test_dot_git_is_not_walked`).
- Deliberate differences: our scanner id `indus-scan-v1` and our own category names/rule ids/corpus. A native binary is `high` supply-chain (reference `binary_file` is critical) — a shipped binary is suspicious but not necessarily run on install; curl-pipe-shell is `high` (forceable), decode-then-execute `critical`. One generic one-step context demotion (docs prose, whole-line comments, test trees, changelogs, data-URI media) in place of the reference's per-pattern demotions and the community-only `sudo`/`bind` note. File-count cap 2,000 (row text) vs the reference's 50.
- Not taken: the install-policy matrix (trust × verdict) and `should_allow_install` — the callers P13-21/P13-26 own it; the scan cache (callers cache by content hash outside the repo, P13-26/28); the optional advisory second scanner (P13-26); the statement-owner denylist demotion (our denylist/comment demotion is simpler).

## Status

Done (2026-10-19). Shared: `safety/threat_scan.py` gains `locate()` / `Hit` and public `TAG_FIRST`/`TAG_LAST` (the injection phrases reuse it; `scan()` unchanged, its tests still pass). 62 scanner tests pass.
