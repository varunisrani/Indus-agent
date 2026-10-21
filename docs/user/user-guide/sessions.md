# Sessions

Every conversation is a session, saved as it happens in the profile's store (an SQLite database
with full-text search). Your message is saved before the model is called and each tool result as
it arrives, so a crash or a killed terminal loses nothing that already happened.

## Finding and resuming

<!-- test -->
```console
$ indus sessions list
No sessions yet: nothing has been saved in this profile.
```

| Command | What it does |
|---|---|
| `indus sessions list` | recent sessions, newest first (`--source telegram`, `--workspace NAME` filter them) |
| `indus sessions browse` | pick one interactively |
| `indus -c` / `indus -c NAME` | continue the newest session, or the one with that title |
| `indus -r ID_OR_TITLE` | resume a session by id or title |
| `/resume`, `/sessions` | switch from inside a chat |
| `/title NAME` | name the current session |

When you resume, Indus Agent goes back to the folder the session worked in unless you pass
`--no-restore-cwd`. The agent itself can look up earlier conversations with its `session_search`
tool, so "what did we decide about the cache last week?" works.

## Branching and taking back

- `/branch` copies the conversation into a new session and continues there; the original stays.
- `/undo [N]` takes back the last N exchanges, `/retry` sends your last message again.
- `/compress` summarises older turns to make room in the context window. Compression also happens
  on its own when the window fills up (`compression.*`).

## Exporting and tidying

```bash
indus sessions export all.jsonl                    # every session, re-importable
indus sessions export notes --format md --title cache
indus sessions import all.jsonl
```

Exports mask API keys, tokens and other secrets unless you pass `--no-redact`. `indus sessions
prune --older-than 90d` deletes old sessions (`--dry-run` first), `archive` moves them out of the
way, `pin` keeps one from pruning, `stats` shows the store's size. `sessions.retention_days` and
`sessions.auto_prune` make pruning automatic.

## Worktrees

`indus -w` starts a session in a fresh git worktree of the current repository, on its own branch,
so the agent's changes never touch your working copy. `indus worktree list` shows them; clean,
merged worktrees older than `worktrees.prune_after_hours` are pruned.

## Checkpoints

With `--checkpoints` (or `checkpoints.enabled: true`) the folder is snapshotted before the agent
changes files. `/rollback` lists the checkpoints, `/rollback diff N` shows what changed since one,
and `/rollback N` restores it. Snapshots live outside your repository and never touch its history.
