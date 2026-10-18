# Task: Persona and project context files (P04-06) Phase: 04 Owner: wave-5 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/prompt_builder.py` (the
bounded file read, the context scan wrapper, git-root and native-file walk, front-matter strip,
truncation with omitted headings, per-context truncation warnings, the persona loader, the four
candidate finders, the AGENTS chain, the install-tree suppression and the context-files assembler),
`tools/threat_patterns.py` (API shape only: scopes, invisible-character check on raw text, NFKC
fold, scan length cap — not the patterns), `website/docs/user-guide/features/context-files.md`.
Reference tests skimmed for edge cases: `tests/agent/test_prompt_builder.py` (chain gaps, dedup of
identical chain content, no git root = cwd only, override file beats AGENTS.md, native file beats
override, install-tree fallback, empty persona, slow native file skipped while AGENTS.md still
loads, warnings isolated across contexts). Closed before writing.
Research: `docs/plan/research/01-agent-core.md` §3.5, trap R01.15, R01.16. Evidence: none.

What it does (own words):
- **Persona** (`persona.py`): only `<home>/PERSONA.md` of the build's profile (never the cwd).
  Missing, empty, whitespace-only, unreadable or a timed-out read → `None` and the built-in
  identity is used. Otherwise the text is scanned (a hit on the user's own file is logged and the
  file still loads — it is the same trust class as the config file), truncated to the cap and
  returned verbatim. Loaded when context files are on or the persona-even-without-context-files
  switch is on. Never repeated in the project-context section.
- **Project context** (`context_files.py`): from the cwd, the first category that yields any
  content wins; lower ones are not looked at:
  1. `.indus.md` / `INDUS.md` — nearest existing file walking from cwd up to the git root (outside
     git: cwd only); YAML front matter stripped.
  2. AGENTS chain — git root first, cwd last; in each directory the personal override file is
     used when it has text, else the committed file, else its lower-case spelling (names in
     `AGENTS_NAMES`); identical content further down is dropped;
     each file gets its own heading; outside git only the cwd. The merged chain is capped again.
  3. `CLAUDE.md` / `claude.md` in the cwd, first with content.
  4. `.cursorrules` plus `.cursor/rules/*.mdc` (sorted) in the cwd, all non-empty ones.
- Files inside the Indus install tree are never project context (cwd inside it → no project
  context, logged).
- Every read runs off the event loop with a deadline (`prompt.context_file_read_timeout`, 5 s
  default) measured on the scope's clock; a late read is abandoned (never joined), logged at
  WARNING and counts as empty, so the next category can still load.
- Every file is scanned with `safety.threat_scan` (context scope); a hit replaces that file's text
  with a one-line blocked marker naming the file and the findings.
- Truncation: cap = `prompt.context_file_max_chars` when a positive int, else
  `window × 4 × 0.06` clamped to [20,000, 500,000]; unknown window → 20,000. Over the cap keep
  70 % head + 20 % tail, with a middle marker giving kept/total chars, up to 15 omitted headings
  (headings inside fenced code are not headings; `…` when more) and the path to read with
  `read_file`. A warning is queued for the chat status line in a context variable that each build
  sets fresh, so two concurrent builds never see each other's warnings.
- **Threat scan** (`safety/threat_scan.py`): `scan(text, scope=)` → finding ids. Invisible and
  bidirectional control characters (and Unicode tag characters) are reported per code point from
  the raw text; patterns run on an NFKC-folded, length-capped copy. Our own pattern families:
  instruction override, hiding things from the user, role takeover, prompt leaking, secret
  exfiltration via network commands, reading secret files, hidden HTML comments/blocks; `strict`
  adds persistence shapes (SSH key planting, shell-profile edits).

Inputs → outputs: cwd, home, window size, config view, clock → rendered project-context text (or
""), persona text (or None), queued warnings; `scan(text)` → tuple of finding ids.

Files: `src/indus/core/prompt/context_files.py`, `src/indus/core/prompt/persona.py`,
`src/indus/safety/threat_scan.py`, `tests/unit/core/prompt/test_context_files.py`,
`tests/unit/safety/__init__.py`, `tests/unit/safety/test_threat_scan.py`,
`tests/fixtures/context_files/trees.yaml`, `tests/fixtures/threat_scan/corpus.yaml`; shared:
`locales/en.yaml` (status-line warning text).

Edge cases / failure modes:
- `.git` may be a file (worktrees, submodules): it still marks the root.
- a parent the process may not stat counts as "no .git there"; unreadable files count as empty.
- empty `AGENTS.override.md` falls through to `AGENTS.md` in the same directory.
- chain directories without any file contribute nothing; the same text in root and subdir loads
  once.
- case-insensitive file systems: `AGENTS.md` and `agents.md` are one file; reading it twice is
  harmless (dedup by content).
- a leading BOM is not an injection (stripped before the scan).
- front matter only (no body) counts as empty.
- slow read on a network file system: skipped after the deadline; the thread is left to finish.

Our design differences / decisions:
- Persona file name `PERSONA.md` (research §2 suggestion, row text).
- Chain headings name the file relative to the git root (`AGENTS.md`, `packages/AGENTS.md`) instead
  of relative to the cwd, so the bytes are the same from any cwd and in every worktree.
- The install-tree guard is unconditional (row text: "never"), not only for fallback cwds; the
  install root is the `indus` package directory, or the checkout root when it sits under `src/`.
- Repo trees for tests are described in one YAML fixture and materialised in a temp dir (a `.git`
  cannot be committed inside the repo, and committed `AGENTS.md`/`CLAUDE.md` files would be picked
  up by coding tools working in this repo).
- No profile-distribution persona blocking (no profile distributions yet; P07/P24 can add it), no
  safe-worker mode, no legacy bot-roster stripping, no persona seeding (P06 bootstrap).
- The bounded read is a private helper here; it should become the shared D-04.1 blocking helper
  when one lands.

Tests we will write: each category from a temp repo; category priority (native over override over
AGENTS over CLAUDE over cursor); chain order root → deepest; gaps; dedup; no git = cwd only; empty
override falls through; install-tree cwd ignored; slow native file hits the deadline on an injected
clock while AGENTS.md still loads; scan hit → marker; truncation at three window sizes plus explicit
config cap; omitted headings ignore fenced code; two concurrent builds keep separate warnings;
persona missing/empty/whitespace/unreadable → None; persona scan hit still loads; scanner corpus.

## Decisions for the lead
- In a dev checkout, running `indus` inside the Indus repo itself loads no project context (the
  install-tree rule is absolute). An opt-out flag can be added if contributors want it.
- Truncation markers name the absolute file path for `read_file`; that is worktree-specific text
  inside the context tier, but only for files over the cap.

Compare checklist (filled after the rewrite; reopened the `agent/prompt_builder.py` context-file
section and the user guide):
- [x] category order and "first category with content wins" (test:
  `tests/unit/core/prompt/test_context_files.py::test_category_priority`)
- [x] native file nearest from cwd up to the git root; cwd only outside git (test:
  `::test_native_file_walks_up_to_git_root_only`)
- [x] AGENTS chain root → cwd, override first, empty falls through, gaps, dedup (tests:
  `::test_agents_chain_order`, `::test_agents_chain_gaps_and_dedup`,
  `::test_override_wins_and_empty_override_falls_through`)
- [x] cursor rules concatenated (test: `::test_cursor_rules_concatenated`)
- [x] scan hit → blocked marker; persona hit only warns (tests: `::test_scan_hit_is_blocked`,
  `::test_persona_scan_hit_still_loads`)
- [x] truncation 70/20 with headings and read_file pointer; cap table (tests:
  `::test_cap_table`, `::test_truncation_marker`)
- [x] slow read skipped, next category loads (test: `::test_slow_read_hits_deadline`)
- [x] warnings per build (test: `::test_concurrent_builds_keep_separate_warnings`)
- [x] gap found: a slow read on the default executor held up `asyncio.run` shutdown (it joins the
  executor) — reads now run on daemon threads that are never joined, pinned by
  `::test_slow_read_hits_deadline` (asserts the read is still stuck when the build returns)
- Deliberate differences: chain and native labels are relative to the git root, not the cwd; a
  front-matter-only native file counts as empty (the reference falls back to the raw text,
  front matter included); install-tree files are refused for every surface, not only for a
  fallback cwd; headings indented four or more spaces are code, not headings; the persona scan
  never blocks (no profile distributions yet).
- Not taken: profile-distribution persona blocking, safe-worker mode, legacy bot-roster
  stripping, persona seeding, subdirectory hints (later phase, appended to tool results), the
  `/context` manifest listing of every discovered file.

## Status

Done (2026-10-18). Shared edit: `locales/en.yaml` (`prompt.context_truncated`). Cross-slice names:
none.
