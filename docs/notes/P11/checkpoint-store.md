# Task: checkpoint store (P11-15) Phase: 11 Owner: P11 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/checkpoint_manager.py`
(header docstring, git env builder, store init, project registration, the snapshot function,
hash and path validators). Research `02-tools.md` §3.26. Closed before writing.
Evidence: none.

What it does (own words): one shadow git store per profile under `<home>/checkpoints/store/`
(a bare repository) holds file snapshots of every working folder the agent edits. Each folder is
a "project" keyed by `sha256(resolved path)[:16]`; its history is the ref `refs/indus/<key>`, its
staging area the index file `indexes/<key>`, its metadata `projects/<key>.json`
`{workdir, created_at, last_touch}`. All projects share one object database, so the same file
content in two worktrees is stored once. Every git call runs with `GIT_DIR` (the store),
`GIT_WORK_TREE` (the folder) and `GIT_INDEX_FILE` (the project's index) and with the user's
global/system git config switched off, so nothing is read from or written to the user's own
repository or config; each call has a 30 s bound.

`snapshot(workdir, reason, session_id=, turn_id=, keep=)` → `Snapshot(commit, ...)` or
`Skipped(reason)`: skipped for a missing folder, the file-system root or the user's home (too
broad), more than 50,000 files (dependency/cache folders not counted), nothing to capture, or no
change since the last snapshot. Files over `checkpoints.max_file_size_mb` (10) are left out after
staging; default excludes (VCS dirs, `node_modules`, virtualenvs, caches, build output, binaries,
media, `.env*`, logs, `.worktrees/`) live in `info/exclude`. A nested repository is stored as a
gitlink, not its files; the checkpoint message says which ones were not captured. Commit hashes
and file paths from callers are validated before they reach git (hex only, never starting with
`-`; relative paths that stay inside the folder). The store implements P07's `CheckpointStore`
protocol (`base`, `list_projects`, `size`, `prune`, `clear`) and registers itself with
`checkpoint_api.set_store_factory`.

Inputs → outputs: a scope (home, clock, env, config) and a folder → refs/objects/index/metadata in
the store; `Snapshot | Skipped`; a per-project ledger `ledgers/<key>.jsonl` (newest 2,000 lines)
of snapshot entries `{commit, at, reason, session, turn, files}`.

Files: `src/indus/backends/checkpoints/__init__.py`, `src/indus/backends/checkpoints/store.py`,
`src/indus/backends/checkpoint_api.py` (default factory), `src/indus/foundation/config/schema.py`
(`checkpoints.*`), `tests/unit/backends/checkpoints/test_store.py`.

Edge cases / failure modes:
- git missing, timeout or failure → `Skipped("git …")` (never raised to a tool); the store keeps
  working for the next call.
- Two processes snapshotting at once: one file lock (`<home>/.checkpoints.lock`) serialises
  snapshots, restores, prunes and clear.
- A project index that no longer matches its ref is rebuilt from the ref tip before staging.
- Session and turn ids ride in the commit message body (`Indus-Session:` / `Indus-Turn:`), so
  they survive history rewrites by pruning.
- Container and remote backends: the caller (trigger) skips them; the store only sees host paths.

Our design differences / decisions:
- Our ref namespace `refs/indus/`, our identity `Indus Agent Checkpoint` set through env vars (no
  config writes for identity), commit dates taken from the scope clock (fake clock in tests).
- The file-count guard prunes excluded folders while counting (the reference counts everything,
  so a project with a large `node_modules` was always skipped).
- The ledger records snapshot entries (an audit trail with session/turn), not agent-written file
  hashes; there is no "safe restore" mode in this phase.
- No legacy (pre-shared-store) migration: we never had another layout (D-16.3).
- `list_projects()` reports each project's `size_bytes` as the bytes only that project holds
  (objects not reachable from any other project's ref) — what deleting it would free.

Tests we will write:
- snapshot creates `refs/indus/<key>`; the user's `.git` (status, refs) is unchanged.
- file limit (small limit through the constructor; default constant 50,000) → skipped.
- excludes honoured (`node_modules`, `.env`, oversize file); unchanged → skipped.
- two projects with the same files share objects (store grows less than 2×).
- invalid hashes and escaping paths rejected; protocol methods (list/size/clear) work.

## Decisions for the lead
- `checkpoint_api.py`: added a default factory that loads the real store lazily (the CLI module
  never imports the store); `set_store_factory(None)` still means "no store". The real factory
  returns None while the profile has no store yet, so a fresh profile still prints "No checkpoints
  have been taken". The protocol itself is unchanged.
- New config rows `checkpoints.*` (enabled false, max_snapshots 20, max_total_size_mb 500,
  max_file_size_mb 10, retention_days 7, min_interval_hours 24, auto_prune true).

Compare checklist (filled after the rewrite; reopened `tools/checkpoint_manager.py`):
- [x] store layout, per-project ref/index/metadata, shared objects (test: `tests/unit/backends/checkpoints/test_store.py::test_two_projects_share_objects`)
- [x] git env isolation incl. global/system config off (test: `tests/unit/backends/checkpoints/test_store.py::test_snapshot_leaves_the_user_repo_alone`)
- [x] 50,000-file skip, too-broad dirs, empty tree, no-change skip (test: `tests/unit/backends/checkpoints/test_store.py::test_file_limit_skips`, `::test_unchanged_is_skipped`)
- [x] max file size dropped after staging; default excludes (test: `tests/unit/backends/checkpoints/test_store.py::test_excludes_honoured`)
- [x] hash/path validators reject `-` prefixes, non-hex, traversal and absolute paths (test: `tests/unit/backends/checkpoints/test_store.py::test_validators`)
- [x] nested repos noted in the message (test: `tests/unit/backends/checkpoints/test_store.py::test_nested_repo_noted`)
- Deliberate differences: see above (dates from the scope clock, excluded dirs not counted, ledger
  is an audit trail, no legacy migration, `--disk-usage` exclusive project sizes).
- Not taken: agent-write ledger / safe restore; volume-identity orphan evidence (orphans are only
  deleted by an explicit, confirmed `indus checkpoints prune`).

## Status

Done (2026-10-18). Shared: `src/indus/backends/checkpoint_api.py` (default factory),
`src/indus/foundation/config/schema.py` (`checkpoints.*`, `worktrees.*`).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- No change at integration; the real store factory stays the CLI default.
