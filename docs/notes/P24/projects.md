# Task: Projects (P24-14) Phase: 24 Owner: wave-37 builder B1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/projects_db.py` (all),
`hermes_cli/projects_cmd.py` (all), `tui_gateway/methods_projects.py` (method list and the
discovery policy helpers), `tools/project_tools.py` (all); research 08 §3.14. Closed before
writing.

What it does (own words): a project is a named workspace a person keeps per profile: a name, a
slug, an optional description, icon and colour, an optional bound board, and the folders it owns
(plain folders, repos, worktrees), one of them primary. Sessions started inside a project folder
join that project (the innermost folder wins when projects nest). One project may be active per
profile. A bare new chat stays detached: it starts in a folder only when a project or a default
directory applies. Repo discovery walks the user's home (or configured roots) to a bounded depth
(3 by default), never following links, skipping hidden folders and excludes, and stops at a folder
that holds `.git`. The desktop sidebar reads all of this through `projects.*` methods; the agent
gets a `project_info` tool; people use `indus project`.

Inputs → outputs: `ProjectStore.open(scope)`; `create`, `list`, `get`, `update`, `add_path`,
`remove_path`, `archive`, `restore`, `use`, `active`, `for_path`, `join_session`,
`start_folder`; `discover_repos(policy, user_home)` → repo roots; CLI `indus project
create|list|use|add-path|remove-path|archive`; RPC `projects.list|get|create|update|add_path|
remove_path|archive|use|for_path|discover`; tool `project_info`.

Files: `src/indus/state/projects.py`, `src/indus/cli/commands/project.py`,
`src/indus/service/projects_rpc.py`, `src/indus/tools/builtin/project.py`,
`src/indus/protocol/projects.py` (method models, not yet in the catalogue),
`src/indus/foundation/config/schema.py` and its test (shared: `projects` and `kanban` rows),
`src/indus/foundation/errors.py` (shared: `projects.*` codes),
`tests/unit/state/test_projects.py`, `tests/unit/cli/commands/test_project.py`.

Edge cases / failure modes: empty name refused; slug collisions get `-2`, `-3`; a folder already
primary in another project refused (switch instead); removing the primary moves primary to the
next folder; paths normalised (absolute, `~` expanded, no trailing separator); `/a/b` is not inside
`/a/bc`; discovery never descends into a repo, a link, a hidden folder or an excluded name, and
stops after a bounded number of folders.

Our design differences / decisions:
- one per-profile file `<home>/projects.db` through the store plumbing and one `Writer`;
- folders carry a `kind` (`folder`, `repo`, `worktree`) instead of a separate table; sessions are
  recorded in `project_sessions` when they join;
- the active project is a meta row; `use` with no argument clears it;
- RPC handlers are plain async functions over pydantic models (`indus.protocol.projects`); adding
  them to the method catalogue and the codegen output is the lead's wiring step;
- discovery results are not cached in the file (the desktop caches its own view).

Tests we will write: discovery honours depth, excludes, hidden folders and stops at a repo (temp
tree); session created inside a project folder joins it (innermost wins); bare chat detached unless
project or default dir; RPC list equals CLI list; slug collision; primary moves on remove; archive
hides from list; CLI non-TTY rows.

## Decisions for the lead
- Wire `PROJECT_METHODS` (in `indus.protocol.projects`) into `indus.protocol.methods.METHODS`, the
  bridge and daemon method tables, and rerun codegen.
- Session creation should call `ProjectStore.join_session(session_id, cwd)` (session service owner).

Compare checklist (reopened the files above):
- [x] create/list/get by id or slug, unique slug, primary dedup (`tests/unit/state/test_projects.py::test_create_and_slug_collisions`)
- [x] add/remove folder and primary repointing (`::test_removing_the_primary_moves_it`)
- [x] project for a path, innermost wins (`::test_session_inside_a_project_folder_joins_it`)
- [x] active pointer set and clear (`::test_active_project_per_profile`)
- [x] discovery depth/excludes/hidden/repo stop (`::test_discovery_honours_depth_and_excludes`)
- [x] RPC list matches CLI list (`tests/unit/cli/commands/test_project.py::test_rpc_list_matches_cli_list`)
- Deliberate differences: folder kinds on one table; no discovered-repos cache table; no
  remote/desktop tree payload.
- Not taken: board-bound deterministic worktree branch names (the board stores `project_id`;
  the branch rule is the worktree owner's), live-session workspace callback (GUI).

## Status

Done (2026-10-21).

## Integration (P24 integrator, 2026-10-21)

Wired: `PROJECT_METHODS` are in the method catalogue (codegen) and served by the daemon's WebSocket
and the TUI bridge; a session created over the WebSocket with a folder inside a project joins it
(`projects_rpc.join_session`; profiles without `projects.db` are left alone).
