# Task: File-ops layer + path resolution (P05-15) Phase: 05 Owner: wave-2 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/file_operations.py`
(sentinel/fenced read protocol, byte-exact read, atomic write script, hash check, line-number
gutter), `tools/file_tools_paths.py` (whole: placeholder cwds, tilde, anchoring, container vs host),
`tools/file_operations_common.py` (result shapes, conflict count, line endings, BOM),
`tools/file_operations_native.py` (host fast path: regular-file open, atomic write, sha256).
Closed before writing.
Evidence: none.

What it does (own words): one object per task environment that the file tools use for every byte
they read or write. When the environment runs commands on the Indus host itself (the local
backend), the object works directly on the file system (open/stat/rename) on the `io` blocking
pool. For every other backend (containers, SSH, cloud sandboxes later) it speaks a small shell
protocol through `Environment.execute(..., bounded=False)`: file bytes travel base64-encoded
between per-call random sentinel lines (`__INDUS_RF_<hex>__`), with the file's own byte count
beside them so stray backend output can never be decoded into the file; writes go in on stdin
(base64, decoded by the remote shell) into a temp file next to the target and are renamed over
it. Capture is always unbounded (R02.2): a bounded capture would cut a large file silently.
Path resolution turns what the model typed into one absolute path: relative paths anchor on the
task's terminal cwd (never the process cwd unless nothing else is known), placeholder cwd values
(`""`, `.`, `./`, `auto`, `cwd`) count as unset, `~` is the home the scope gives children, and
host-only checks (stat, FIFO, realpath) run only on the host path.

Inputs → outputs: `file_ops_for(env | None, scope)` → `FileOps` (`read_bytes`, `write_bytes`,
`stat`, `sha256`, `list_dir`, `cwd`, `host`); `paths.resolve(raw, base, host=…)`,
`paths.base_dir(...)`, `paths.clean_cwd(raw)`, `paths.expand_tilde(raw, scope)`. Errors are
`FileOpError(kind=missing|not_regular|is_dir|failed)`.

Files: `src/indus/tools/builtin/file_ops.py`, `src/indus/tools/builtin/paths.py`,
`tests/unit/tools/builtin/test_file_ops.py`.

Edge cases / failure modes:
- 3 MB file round trip byte-exact on both paths (binary bytes, CRLF, no trailing newline).
- A file whose text contains a sentinel-shaped string (`__INDUS_RF_…`) still parses: the payload
  is base64, which cannot contain `_`, and the sentinel is fresh per call.
- Login-shell noise before/after the fenced region is ignored; noise inside the fence breaks the
  size check and the read is refused ("garbled"), never written back.
- No `base64` binary on the backend (rc 127) → hex transport via `od`.
- FIFO / device on the host: opened `O_NONBLOCK` and `fstat`-checked, so a read never blocks.
- Write to a symlink writes the link's target (the link survives); parents are created; an
  existing file keeps its mode; a new file gets the umask default.

Our design differences / decisions:
- One async interface for both paths (handlers are async; host work runs on the `io` pool).
- The environment comes from the call's `environments` keyword (an `EnvironmentRegistry` the host
  offers through `ToolContext.extras`); with no registry, or no live environment for a `local`
  backend, the host fast path is used with the configured `terminal.cwd` as base.
- Per-env objects are cached in a weak map keyed by the environment object, so they drop with
  the environment (plus an explicit `forget(env)`).

Tests we will write:
- Parametrised over host and a test shell environment (real `bash`, flagged non-host): 3 MB
  round trip, sentinel text in content, missing → `missing`, directory → `is_dir`, stat, sha256,
  symlink target write, parents created, mode kept.
- Shell protocol: noise outside the fence ignored; noise inside → refused; od fallback.
- Paths: `.`/`auto` cwd unset; `~` from the scope's HOME; relative anchors on the env cwd; process
  cwd only as the last resort; container paths never touch the host (`normpath`, no realpath).

## Decisions for the lead
- The environment registry reaches file tools as the handler keyword `environments` (extras key
  `environments`); the host (P06/service) must put it into `ToolContext.extras` next to `broker`.
  Until then the tools use the host fast path anchored at `terminal.cwd` / process cwd.

Compare checklist (filled after the rewrite; reopened `tools/file_operations.py`,
`tools/file_tools_paths.py`, `tools/file_operations_native.py`):
- [x] Sentinel-fenced reads with a byte-count check, noise outside the fence ignored and noise
  inside refused (tests: `tests/unit/tools/builtin/test_file_ops.py::test_noise_inside_the_fence_is_refused_as_garbled`,
  `::test_sentinel_shaped_text_inside_the_file_does_not_break_parsing`)
- [x] Unbounded capture on every shell call, R02.2 (`::test_shell_calls_are_never_bounded`)
- [x] Hex fallback when base64 is missing (`::test_od_transport_when_base64_is_missing`)
- [x] Stdin-delivered atomic writes, mode kept, symlink target written, parents made
  (`::test_write_creates_parents_and_keeps_mode_and_symlink`); byte-exact 3 MB round trip on both
  paths (`::test_three_megabyte_round_trip_is_byte_exact`) and through the real local wrapper
  (`::test_shell_protocol_through_the_real_local_backend`)
- [x] Placeholder cwds unset, `~` from the scope, process cwd last
  (`::test_placeholder_and_relative_cwds_count_as_unset`, `::test_tilde_uses_the_scope_home`,
  `::test_dot_cwd_falls_back_to_the_configured_then_the_process_cwd`)
- [x] FIFO never opened on the host (`::test_fifo_is_not_read`)
- Deliberate differences: the reference passes base64 through text in both directions and has a
  separate native fast path per method; we keep one async interface with two implementations.
  Our writes ship base64 on stdin (the contract's stdin is text) and decode in the remote shell.
  `indus.tools` may not import `indus.backends` (independent layers in the import-linter
  contract), so `file_ops` restates the environment shape it needs as a `Protocol` and tells
  backend failures apart by their `backend.*` error code.
- Not taken: Windows/MSYS path translation (D-16.55), SSH remote-home detection (P11), workspace
  divergence warning for relative paths that leave the cwd (cheap to add with P11 worktrees).

## Status

Done (2026-10-18). Added after the coordinator's notes: `FileOps.delete`, the per-call `cancel`
signal (handler keyword `cancel`, extras key `cancel`), and the extras key `environments`.

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- Tools sit above backends now (D-04.7), so `file_ops` imports `Environment`, `ExecResult` and
  `CancelSignal` from `indus.backends.contract` instead of restating them (four structural
  protocols removed). D's narrower `exec_host.ExecHost` stays for the search and lint helpers.
- New `environment_of(ops)`: the live environment behind a `FileOps` (a cached host object made
  for a live local environment keeps it as `host_env`); lint-after-edit runs its shell checkers
  there. Host access without a live environment has none, so `node --check` / `tsc` are skipped
  with a note until the host wires `environments` (P06). Pinned in
  `tests/unit/tools/builtin/test_file_ops.py::test_file_ops_for_picks_host_or_shell_and_caches`.
- `environments` and `cancel` now reach handlers from the tool round when the session has them
  (`SessionRuntime.environments` is the P06 seam).

## Wave 47 (W47-b): a session's own folder

Status: done (2026-10-22, wave-47 builder b; uncommitted, for the lead's `git apply --3way`).

Read: none (a wiring bug in our own code; no reference file or Evidence opened).

Bug: a desktop chat's file tools resolved relative paths against the daemon's work folder, not
the chat's folder (the window's Terminal pane used the right one). Two causes:
1. Daemon side: `session.create {cwd}` set `AgentOptions.cwd`, which only reached the prompt
   context. The tool round handed tools the host scope, so `paths.base_dir` and `open_local` fell
   back to `terminal.cwd`, else the process folder. A stored session attached again
   (`_attach_existing`: a daemon restart or another process) lost even that.
2. Desktop side: only the sidebar's own "new chat" set the folder of the next draft. A draft
   opened from app start, Ctrl+N, `/new` or the command center sent no `cwd`, while the tool
   panes fell back to the profile's default directory (see `docs/notes/P20/sidebar.md`).

Fix:
- `AgentOptions.workdir` / `SessionRuntime.workdir`: the session's own folder. The tool round gives
  tools `with_workdir(scope, workdir)` (in `backends/task_overrides.py`, beside `LayeredConfig`),
  a scope whose config answers `terminal.cwd` with that folder. File tools (`ops_for_call` with
  no live environment uses `paths.base_dir`), new local shells (`open_local`) and everything that
  reads `terminal.cwd` start there. Subagents inherit it (a worktree child keeps its tree).
- `SessionService._options_for` sets `workdir` with `cwd` when a session is created in a folder;
  `_stored_folder` restores the stored row's `cwd` for a session attached again; the session
  mutations shaper (workspace move, ACP) sets `workdir` with `cwd`.
- Desktop: `freshDraftFolder` (sidebar `projects.ts`) is the one rule for a fresh draft's folder:
  the sidebar's choice, else the profile's default directory. The composer slot creates the
  session with it, and the Shell's tool-pane folder uses the same function.

Tests (fail-first on base `9a155bbb9a`):
- `tests/unit/service/test_session_workdir.py::test_a_relative_write_lands_in_the_chat_folder`
  (failed on base: the file landed in the process folder) and
  `::test_a_session_attached_again_keeps_its_folder` (failed on base); the control
  `::test_a_session_without_a_folder_keeps_the_profile_terminal_cwd` passes on both.
- `apps/desktop/src/sidebar/projects.test.ts` "a fresh draft not started from the sidebar opens in
  the default directory (W47-b)" (failed on base).

### Decisions for the lead
- **W47-b-1 (D-16.1034):** a session's own folder beats the profile's `terminal.cwd` for host tools: the person
  picked the folder for this chat. Only the local backend gets the overlay; a sandbox (docker,
  ssh, cloud) reads `terminal.cwd` as a path inside it, so it keeps the profile's value.
- **W47-b-2 (D-16.1035):** a stored session attached again gets its stored folder only when its source is a
  surface a person starts sessions from (`cli`, `tui`, `gui`, `acp`, `web`) and the folder differs
  from the host's own. Messaging chats keep the host's folder and `terminal.cwd` as before.
- **W47-b-3 (D-16.1036):** a folder that is not an existing absolute directory (deleted since) leaves the
  scope unchanged, so the tools fall back to `terminal.cwd`, else the process folder. No refusal
  and no notice yet (follow-up below).
- **W47-b-4 (D-16.1037):** no new attack surface. `session.create` already accepted `cwd` from an
  authenticated client, and file tools already took absolute paths; only the relative-path anchor
  moves. No threat-model row cites this path.

### Follow-ups
- A notice when a chat's folder no longer exists (today it silently uses the fallback).
- Session rows written before this fix: a desktop chat started without a folder stored
  the daemon's folder as its `cwd`; those chats stay there (the folder equals the host's own).

## Wave 48 (W48-c): a notice when a chat's folder is gone

Status: done (2026-10-22, wave-48 builder c; uncommitted, for the lead's `git apply --3way`).

Read: none (a follow-up of W47-b-3 in our own code; no reference file or Evidence opened).

What changes: W47-b-3 left the tools of a chat whose folder was deleted falling back to
`terminal.cwd` (else the process folder) without a word. Now the tool round says so: when the
session has its own folder, the local backend would use it, and it is not an existing directory,
the turn emits one `notice` event (level `warning`, key `workdir_missing`) naming the folder:
"This chat's folder no longer exists: <folder>. Its tools run in the default folder instead." The
fallback itself is unchanged.

- `backends/task_overrides.py`: `workdir_gone(scope, folder)`, the same conditions as
  `with_workdir` turned around (absolute, local backend, not a directory). A relative or empty
  folder is not "gone" (it was never used); a sandbox backend never uses the host folder.
- `core/tool_round.py`: before `with_workdir`, at most once per turn (`TurnState.workdir_noticed`).
  The notice comes only when tools run, i.e. when the fallback is actually used; a turn without
  tool calls says nothing. The text is `loop_texts.WORKDIR_GONE_NOTICE`.
- Every surface already shows `notice` events: the desktop transcript keeps one row per key
  (`messageStream.ts` upserts by key) and the TUI shows its notice line (the web chat runs the
  TUI). No protocol change.

Tests (fail-first on base `1b6e711`):
`tests/unit/service/test_session_workdir.py::test_a_chat_whose_folder_is_gone_says_so_once_per_turn`
(failed on base: no notice; with the fix: two tool rounds, one warning naming the folder, the
files land in the fallback folder; a chat whose folder exists gets none).

### Decisions for the lead
- **W48-c-1 (D-16.1076):** the "folder gone" notice is a turn event (`notice`, `warning`, key `workdir_missing`),
  sent once per turn and only when a tool round runs, not a refusal: the person keeps working in
  the default folder and sees why relative paths land there. Desktop rows upsert by key, so a
  long chat shows the notice once (where it first appeared) until the window reloads.

### Follow-ups
- Moving a chat to another folder from the notice (a "Choose folder" action) needs a desktop
  control over the workspace-move mutation; not built here.
