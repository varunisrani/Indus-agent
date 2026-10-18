# Task: Dangerous-command detection (P05-07) Phase: 05 Owner: wave-1 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/approval_detection.py` (pattern
*labels* only, extracted without the regex bodies; the normalisation helpers: unicode/ANSI/NUL
clean-up, continuation joins, home folding, `${x:-d}` resolution, IFS collapse, wrapper word table
and option arities, interpreter exec flags, read-tool exec options, parser limits, the order of the
hardline and dangerous passes), `website/docs/user-guide/security.md` (§Hardline Blocklist,
§What Triggers Approval, §Content-Level Command Checks). Test names and the case lists of
`tests/tools/test_approval.py` detection classes (behaviour only). Research `02-tools.md` §3.13,
traps R02.23. Closed before writing.
Evidence: none.

What it does (own words): looks at a shell command the model wants to run and says whether it is
harmless, dangerous (needs a human's approval) or hardline (never runs, whatever the mode). The
approval pipeline (P05-08) uses `hardline` as a floor below every bypass and `dangerous` +
`pattern_key` for prompts and session approvals. It has to see through the ways a command can be
disguised: wrappers (`sudo env nice rm …`), shell and interpreter payloads (`bash -c "…"`,
`python -c "os.system('…')"`), quoting tricks (`r''m`, `r\m`), `$IFS`, `${x:-/}`, absolute home
paths, unicode look-alikes, ANSI noise, base64-decode-and-run. A command we cannot parse is
treated as dangerous and hardline (fail closed).

Inputs → outputs: `detect(command, *, user_home=None, indus_home=None) -> Detection` where
`Detection{dangerous, hardline, pattern_key, description, trace}`; `trace` is a tuple of short
steps saying how the verdict was reached (segment, wrappers unwrapped, payload depth).
Homes default to the current scope (`scope.home`, `HOME` from the scope env) when not given.

Design (ours, not the reference's regex list):
1. Limits first: oversized text, too many separators, nesting deeper than the limit → hardline
   `parser_limit`.
2. Hidden characters: zero-width/bidi controls (not emoji joiners) → dangerous `hidden_unicode`.
3. Normalise: NFKC, strip ANSI CSI/OSC and 8-bit C1 sequences, drop NULs, join `\`-newline,
   `$IFS` forms → space, resolve simple `${name:-literal}` / `${name/a/b}`.
4. Lex with a small shell lexer of our own: simple commands with words (quotes removed, escapes
   applied), operators (`| || && ; & newline`), redirections, here-doc bodies, and nested scripts
   from `$(…)`, backticks and `<(…)`/`>(…)`. Unterminated quotes/substitutions → hardline
   `malformed`.
5. Per simple command: drop leading `NAME=value` (kept for daemon-redirect checks), unwrap
   wrappers with their option arities, fold homes in words, lowercase the program name (flags keep
   their case), then run category rules; payloads of `sh -c`, `eval`, interpreters' `-c`/`-e`, `env
   -S`, `find -exec`, `xargs`, `rg --pre`, `sort --compress-program` are analysed recursively
   (depth limit 6). Pipelines are checked for fetch→shell, decode→shell, secret-file→upload.
6. The worst finding wins (hardline over dangerous; first in rule order otherwise).

Hardline keys: `rm_root`, `rm_system`, `rm_home`, `mkfs`, `block_device_write`, `fork_bomb`,
`kill_all`, `power`, plus `parser_limit`, `malformed`.
Dangerous keys: `rm_recursive`, `rm_system_file`, `find_delete`, `xargs_delete`,
`chmod_world_writable`, `chown_root_recursive`, `chmod_exec_run`, `disk_tool`, `sql_destructive`,
`system_config_write`, `user_secret_write`, `indus_config_write`, `project_env_write`,
`service_change`, `kill_force`, `kill_self`, `remote_exec`, `decode_exec`, `metadata_endpoint`,
`secret_upload`, `hidden_unicode`, `heredoc_exec`, `git_destructive`, `package_remove`,
`docker_remote`, `docker_lifecycle`, `sudo_privilege`, `indus_lifecycle`, `dynamic_word`.

Files: `src/indus/safety/approvals/__init__.py`, `src/indus/safety/approvals/detect.py`,
`src/indus/safety/approvals/shlex_lite.py` (the lexer), `tests/unit/safety/approvals/test_detect.py`,
`tests/fixtures/approvals/corpus.yaml`; shared: `docs/plan/16-what-we-change.md` (D-16.76
"Decided in").

Edge cases / failure modes:
- Benign dev commands stay benign: `rm -rf ./build`, `rm -rf node_modules dist`, `git reset --soft`,
  `grep -r "rm -rf /" .`, `git commit -m "drop table users"`, `echo "reboot"`, `find . -name '*.pyc'
  -print`, `chmod 755 run.sh`, `git branch -d merged`, `npm uninstall lodash`, `cat .env > b.txt`.
- Quoted prose is data unless a shell/interpreter runs it.
- `rm -rf $DIR/` (variable that may be empty) → dangerous; `rm -rf ${x:-/}` → hardline.
- A recursive delete of any absolute path is dangerous (no `/tmp` exemption); only relative paths
  inside the working tree without `..`, globs at the top level or variables are benign.
- `kill -9 1234` dangerous, `kill -9 -1` hardline, `kill 1234` benign.
- `dd` only counts when a side is a device (`/dev/null`, `/dev/zero`, `/dev/urandom` as input are fine).
- Here-doc into a shell or interpreter: dangerous, and the body is analysed (hardline inside stays
  hardline).
- Unparseable quoting → hardline `malformed`; shell-valid escapes inside double quotes parse.

Our design differences / decisions:
- Token-level rules on a parsed command instead of a regex list (D-16.76): quoted prose is
  naturally data, so no quote-masking variants are needed.
- No PowerShell / cmd.exe families yet (POSIX shells first; Windows terminal is later).
- The reference documents "every `bash -c` asks", its code does not; we follow the code and
  analyse the payload instead (R02.23).
- `rm -rf ./build` is benign for us (the row's corpus); the reference asks for every recursive rm.
- No exemption for the agent's own temp verification scripts (we create none yet).

Tests we will write:
- corpus-driven (≥ 300 rows, verdict + key) from `corpus.yaml`;
- unicode/ANSI/NUL/fullwidth obfuscations and invisible characters (built in Python);
- wrapper unwrapping with option arities, `-c` recursion, depth limit;
- `echo … | base64 -d | sh`; unparseable quoting; parser limits; home folding with injected homes;
- trace content; seeded-random fuzz: the detector never raises and stays bounded in time.

## Decisions for the lead
- `Detection.pattern_key` values are our own short keys (listed above); P05-08 session approvals
  key on them.
- Unparseable quoting is hardline (the floor fails closed) as in the reference's floor.

Compare checklist (filled after the rewrite; reopened `tools/approval_detection.py`, the docs):
- [x] every hardline label has a key and corpus rows (root/system/home deletes, mkfs, dd and
  redirects to block devices, fork bombs, kill-all, shutdown/reboot/init/telinit/systemctl power)
- [x] every POSIX dangerous label has a key and corpus rows (deletes incl. flags after operands,
  find/xargs, dynamic words, chmod/chown, disk tools, SQL, system/credential/project/Indus writes
  by redirect, tee, cp/mv/install/ln, in-place sed/perl/ruby, services incl. launchd, kill
  variants incl. regex and self-kill through pgrep/pidof, remote exec by pipe, process and command
  substitution, decode-and-run (base64, xxd, tr, openssl, gunzip, printf escapes), metadata
  endpoints, secret uploads and piped credential uploads, hidden unicode, here-documents, git,
  chmod +x then run, sudo flags, package removal, docker/podman daemon redirects and lifecycle,
  gateway lifecycle and update)
- [x] normalisation steps: ANSI/8-bit C1, NUL, NFKC, continuations, `$IFS` forms, `${x:-d}`,
  `${x/a/b}`, home folding (`test_detect.py::test_obfuscated_spellings_hit_the_floor`,
  `::test_home_folding_uses_given_homes`)
- [x] gap: literal `$(echo word)` / backtick program words — resolved in `normalise`, pinned by
  corpus rows `$(echo rm) -rf /` and `` `echo reboot` ``
- [x] wrapper table and arities; interpreter exec flags incl. `deno eval` (corpus rows under
  "hardline through wrappers and payloads")
- [x] parser limits and malformed quoting fail closed (`::test_size_limits_fail_closed`,
  `::test_malformed_is_hardline_with_reason`, `::test_payload_recursion_depth_limit`)
- Deliberate differences: rules run on a parsed command (our lexer, `shlex_lite.py`) instead of
  regex variants of the text, so quoted prose is data without quote masking; `rm -rf ./build`
  and other relative in-tree deletes are benign; `dd` and redirects count only with a device;
  interpreter payloads are scanned for shell commands only when they call an exec API, and
  tree-delete calls (`shutil.rmtree`, `fs.rmSync`, …) are read as `rm -rf`.
- Not taken: PowerShell and cmd.exe families (Windows terminal later); the exemption for the
  agent's own temp verification scripts (we create none); deleting the running interpreter or venv
  (that floor belongs to the approval policy, P05-08).

## Status

Done (2026-10-18). `detect(command, *, user_home=None, indus_home=None) -> Detection` exported from
`indus.safety.approvals`; keys and descriptions in `detect.HARDLINE` / `detect.DESCRIPTIONS`;
corpus `tests/fixtures/approvals/corpus.yaml` (590+ rows). Shared: D-16.76 "Decided in" filled in
`docs/plan/16-what-we-change.md`. P05-08 builds the policy (floors first) on top.
