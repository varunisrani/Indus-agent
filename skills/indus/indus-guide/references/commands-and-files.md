# Indus Agent: commands and files

A map to start from. Confirm any detail with `indus --help` or `indus <command> --help` on the
installed version.

## Top-level commands

| Command | What it is for |
|---|---|
| `indus` / `indus chat` | Start a chat in the terminal (`-z "<prompt>"` for one answer and exit, `-c` to continue the last session, `-s <skill>` to preload a skill). |
| `indus setup` | Guided first-time setup: provider, key, model. |
| `indus model` | Pick the main model and provider. |
| `indus fallback` | The ordered list of backup models used when the main one fails. |
| `indus auth` | Add, list and remove provider keys and sign-ins. |
| `indus config` | `show`, `get`, `set`, `unset`, `edit`, `path`, `env-path`. |
| `indus tools` | `list`, `enable`, `disable` toolsets per surface. |
| `indus approvals` | Check what the approval rules would do with a given command. |
| `indus profile` | `list`, `use`, `show`, `create`, `delete`, `rename`, `describe`, `alias`. |
| `indus sessions` | List, browse, rename, pin, export, archive and prune past conversations. |
| `indus checkpoints` | Inspect and trim the file snapshots taken before edits (`/rollback` in a chat restores one). |
| `indus worktree` | Create, list and prune git worktrees used for sessions. |
| `indus status` | What is configured and whether it is ready. |
| `indus doctor` | Check the installation and settings; `--fix` repairs what it safely can. |
| `indus logs` | Show and follow the log files. |
| `indus backup` / `indus import` | Save the home to a zip / restore one. |
| `indus dump` | A setup summary (secrets masked) to share when asking for support. |
| `indus completion` | Shell completion script. |

Learning features add `indus skills`, `indus memory`, `indus curator`, `indus journey` and
`indus bundles` (check `--help` for the verbs your version has).

## Slash commands inside a chat

`/help` lists them all. Common ones: `/new` (fresh session), `/resume`, `/model`, `/reasoning`,
`/compress` (summarise older turns to free context), `/usage` (tokens and cost), `/retry`,
`/undo`, `/title`, `/tools`, `/skills`, `/memory`, `/learn` (make a skill from a folder, a link
or this conversation), `/refine` (review this session for things worth remembering), `/curator`.
Any installed skill runs as `/<skill-name> [instruction]`.

## Files in a profile home

The default home is `~/.indus` (override with `INDUS_HOME`); named profiles live under
`~/.indus/profiles/<name>/` and have the same layout.

| Path | Holds |
|---|---|
| `config.yaml` | Settings (edit with `indus config`). |
| `.env` | Environment values for this profile, such as API keys. |
| `PERSONA.md` | The agent's standing instructions and voice; the user owns it. |
| `memories/MEMORY.md`, `memories/USER.md` | The agent's notes and the user profile, written with the `memory` tool. |
| `skills/` | Installed skills, one folder each, grouped by category. |
| `skill-bundles/` | YAML files that group skills under one slash command. |
| `sessions/`, `logs/`, `cache/` | History, logs and scratch space. |
| `.no-bundled-skills` | When present, the bundled skills are not copied into this profile. |

## Settings people ask about

| Key | Meaning |
|---|---|
| `model.default`, `model.provider` | Main model and provider. |
| `agent.max_turns` | Model calls per turn before the agent stops to summarise. |
| `terminal.backend`, `terminal.cwd` | Where commands run and in which folder. |
| `memory.notes_enabled`, `memory.profile_enabled` | Switch the two memory stores on or off. |
| `skills.external_dirs` | Extra folders to load skills from. |
| `skills.auto_load` | Skills loaded into every new session. |
| `curator.enabled` | Idle-time tidying of skills the agent wrote itself. |
