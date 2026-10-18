# Task: search_files (P05-21) Phase: 05 Owner: wave-2 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/file_operations_search.py`
(whole: modes, ripgrep and find/grep paths, ordering, admission, macOS exclusions, newline regex,
timeouts, zero-match probes). Closed before writing. Ripgrep's flags and `--json` output are a
third-party interface; we use them as ripgrep 14 documents them (`rg --help`, `--json` message
types begin/match/context/end/summary).
Evidence: none.

What it does (own words): one tool finds things in the task's file tree. `target: content`
searches file contents with a regular expression through ripgrep and answers either the matching
lines (with optional context lines), only the file names, or a count per file. `target: files`
lists files whose name matches a glob, in the order ripgrep finds them (fast, stops at the page
end) or newest first (`order: modified`, which has to look at the whole tree). Results page with
`limit`/`offset`. The search runs in the task's terminal environment, so a sandboxed task searches
its sandbox. Hits in credential and env files are dropped and counted. Broad searches from a
macOS home skip the privacy-protected folders so no permission prompt pops up unattended. Two
file-name searches over the same root never run at once; the second waits in 50 ms steps and gives
up if the turn is stopped. A search that hits its time limit returns what it had with
`limit_reason: "search_timeout"`.

Inputs → outputs: `{pattern, target (content|files; grep/find accepted), path (.), file_glob,
limit (50), offset (0), order (discovery|modified), output_mode (content|files_only|count),
context (0)}` → JSON `{target, output_mode, matches | files | counts, total_count, shown,
truncated, next_offset?, limit_reason?, filtered?, notes?}` or `{error}`.

Files: `src/indus/tools/builtin/search_files.py`, `src/indus/tools/builtin/exec_host.py` (shared
with P05-20), `tests/unit/tools/builtin/test_search_files.py`; shared: one line in
`src/indus/tools/builtin/__init__.py` (BUILTIN_MODULES), `docs/internals/acceptance-map.md`
(ACC-C13-1).

Edge cases / failure modes:
- `rg` missing in the environment → error naming ripgrep and `indus doctor`.
- Path missing → error; pattern starting with `-` (passed with `-e`); path starting with `-`
  (after `--`); `~` paths expand in the environment's shell.
- Regex with `\n` (odd backslashes) or a literal newline → ripgrep multiline mode plus a note.
- ripgrep exit 2 with some output (one unreadable file) → results kept plus a note; exit 2 and no
  output → error with ripgrep's message.
- Output cut at the page bound (`head -n`) ends ripgrep with SIGPIPE (141 under pipefail): that
  is a full page, not an error.
- Timeout → partial page, `truncated`, `limit_reason`.
- `.env`, `.env.*` (templates such as `.env.example` kept), `*.env`, `.envrc`, `.netrc`,
  `.pgpass`, `.git-credentials`, `.npmrc`, `.pypirc`, `.aws/credentials`, `.ssh/id_*` and the
  Indus home's secret files are filtered from every mode.
- `order: modified` needs ripgrep ≥ 14 (`--sortr=modified`); older → error suggesting discovery.
- Filename pattern without glob characters is searched as `*pattern*` (case-insensitive glob).

Our design differences / decisions:
- Content search parses `rg --json` (no fragile `path:line:` splitting); the grep/find fallbacks
  are not built: ripgrep is required (doctor checks it, D-16 style "one engine").
- Matching directories are not added to file-name results (rg lists files only).
- No zero-match probes (case/hidden/literal hints) yet — could be added later.
- The environment comes from the tool context (`environments` registry, task/session id); the
  command runs with `bounded=False` (R02.2) and the page bound is applied in the shell.

Tests we will write:
- Real local environment over a temp tree (skipped when rg or bash is missing): each output mode;
  paging and `next_offset`; context lines; `.env` hit filtered; modified order; files discovery;
  `rg` missing (command name overridden, see the test note); missing path; pattern starting
  with `-`; newline regex note.
- Fake host: timeout marker, exit 2 handling, SIGPIPE page, admission waits and gives up on
  cancel (fake clock), macOS exclusions table.

## Decisions for the lead
- Tool context keyword `environments` (the `EnvironmentRegistry`) and `cancel` are what the
  handler asks for; the tool round offers only `broker` today. Wire both when P05-11/15 add theirs
  (same names for the terminal and file tools would be simplest).
- Toolset name `file` (P05-24 owns toolsets).

Compare checklist (filled after the rewrite; reopened `tools/file_operations_search.py`):
- [x] Output modes and paging (tests: `test_search_files.py::test_files_only_and_count_modes`,
  `::test_paging_with_next_offset`, `::test_content_mode_lines_and_context`)
- [x] Filename search: discovery bounded by the page, `modified` exact newest first, ripgrep ≥ 14
  check (tests: `::test_file_name_search_discovery_and_modified`,
  `::test_old_ripgrep_refuses_modified_order`)
- [x] One filename search per (backend, root); the waiter gives up when stopped (tests:
  `::test_second_file_name_search_waits_and_gives_up_on_stop`,
  `::test_waiting_search_runs_once_the_first_finishes`)
- [x] Timeout keeps partial results with `limit_reason: search_timeout`; exit 2 with results is a
  note, without results an error; a SIGPIPE at the page bound is a full page (tests:
  `::test_timeout_keeps_partial_results_with_a_marker`, `::test_exit_two_with_and_without_results`,
  `::test_page_bound_sigpipe_is_a_full_page_not_an_error`)
- [x] Newline in the regex turns on multiline mode with a note
  (test: `::test_missing_path_dash_pattern_and_newline_regex`)
- [x] macOS exclusions only below a broad root, never for an explicit protected root, and only
  for a local host (tests: `::test_macos_exclusions_only_below_a_broad_root`,
  `::test_macos_exclusions_reach_the_command_on_a_local_mac`)
- [x] Gap found while testing: dropping credential files after the page bound could hide a next
  page — fixed by reading `FILTER_SLACK` extra results and offering `next_offset` when results
  were dropped from a cut read, pinned by
  `::test_dropped_results_at_the_page_bound_still_offer_a_next_page`
- Deliberate differences: `rg --json` instead of `path:line:` text parsing; ripgrep required
  (no grep/find fallbacks); file-name globs are case-insensitive (`--iglob`) and a bare word is
  `*word*`; `--no-config` so a user's ripgrep config file cannot change the output shape; paths
  lose the `./` ripgrep adds for the default root.
- Not taken: grep/find fallbacks, the directory supplement for file-name search, zero-match
  probes (case / hidden / literal hints), multi-path recovery from one `path` string, the
  per-environment cache of the resolved `rg` path (one `command -v` per call instead).
- Test note: "rg missing" is tested by pointing the module's `RG` command name at a name nobody
  has; a PATH override cannot hide `/usr/bin/rg` because the local backend completes PATH with
  the standard bin folders.

## Status

Done (2026-10-18). Tool registered in `BUILTIN_MODULES` (toolset `file`); 40 tests in
`test_search_files.py` (the real-environment ones skip when `rg` or `bash` is missing). Goldens
under `tests/golden/prompt_surface` now differ by the new tool (9 failures) — left for the lead to
regenerate at merge, as instructed. The handler needs the tool context to offer `environments`
(and `cancel`); until then it answers "no terminal environment".

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- `search_files` gets `environments` and `cancel` from the tool round once the host sets
  `SessionRuntime.environments` (P06 seam); until then it answers "no terminal environment is
  available". The host should give it the same registry as the terminal
  (`terminal.runtime().environments`) so both see one cwd.

P08 wave 1 integration (lead, 2026-10-18): `::test_paging_with_next_offset` failed now and then
(once in a wide run, then 3 of 21 runs alone): ripgrep's parallel walker reports files in a
different order on each run, so page two (a second `rg` call with `offset`) could repeat a hit of
page one and miss another — a real paging bug, not only a test flake. Content search now runs with
`--sort=path` (one stable order; D-16.108); 25 of 25 runs alone pass after the change. File-name
search keeps `order: discovery` (documented to the model as "fast, as found").
