# Task board

The task board is a shared to-do list your profiles hand work to each other on. You (or an agent)
create a task and give it to a profile; a dispatcher starts a one-shot worker for that profile in
the task's workspace, and the worker ends its run by completing the task, blocking it with a
reason, or asking for review.

```bash
indus kanban create "Update the changelog" --assignee writer --body "Cover every merged PR since 1.2"
indus kanban                     # list the tasks, highest priority first
indus kanban show <task-id>      # links, comments, runs and recent events
```

## Boards and tasks

Every profile under the same Indus root shares the boards. A board is one file,
`<root>/boards/<slug>.db`; the first one is `default`. `indus kanban board` lists the boards,
`indus kanban board <slug>` makes another one current (creating it when new), and
`--board <slug>` before a verb uses another board for one command.

A task has a title, an optional body, an assignee (a profile name), a priority (higher runs first)
and a workspace:

| `--workspace` | Where the worker runs |
|---|---|
| `scratch` (default) | a folder of the task's own under the board's folder |
| `worktree:REPO` | a new git worktree of the repository at `REPO` |
| `dir:PATH` | an existing folder |

Tasks can depend on others (`--parent <id>`, repeatable). A task whose parents are not all done
waits in `todo` and becomes `ready` once they are. The other statuses are `running` (a worker
holds it), `blocked` (needs a person), `review`, `done` and `archived`.

## Working on tasks by hand

| Command | What it does |
|---|---|
| `indus kanban move <id> ready\|todo\|blocked\|done\|archived [--reason TEXT]` | move a task yourself |
| `indus kanban assign <id> <profile>` | give a task to a profile (`none` takes it away) |
| `indus kanban comment <id> "text"` | add a comment |
| `indus kanban attach <id> <file>` | attach a file (at most 25 MiB) |
| `indus kanban review <id> --approve` | accept work waiting in review: the task is done |
| `indus kanban review <id> --changes "what to fix"` | send it back with the changes |
| `indus kanban archive <id>` | archive a task (a running worker's run is closed) |

Nothing here asks a question, so every verb works the same in scripts; `--json` prints
machine-readable output on the verbs that show data.

## Workers

The gateway daemon runs a dispatcher pass every `kanban.dispatch_interval_seconds` (60) while
`kanban.dispatch_in_gateway` is on (the default). Without a running gateway, `indus kanban dispatch`
runs one pass by hand (`--dry-run` only says what would start).

A pass starts a worker for each `ready` task that has an assignee, by priority. Tasks without an
assignee, or whose assignee is not a profile on this machine, are skipped. A worker is a one-shot
`indus chat` run of the assignee's profile with the board tools; it never gets the dispatching
profile's provider keys. At most `kanban.max_in_progress` workers run at once; `0` (the default)
works it out from memory: one per 512 MB, between 2 and 8.

How a run ends decides what happens next:

- the worker completed, blocked or asked for review: the task moves on;
- the worker exited without doing any of those: the third time in a row the task is blocked;
- the provider was rate limited or overloaded: the task goes back to the queue and that profile
  waits five minutes, without counting a failure;
- the provider refused the key or model: the task is blocked at once;
- any other failure counts; `kanban.failure_limit` (2) failures in a row block the task.

A claim lasts 15 minutes unless the worker reports it is still working. Each task keeps one log
file under `<root>/boards/<slug>/`.

## The agent and the board

The board tools come in two toolsets. `kanban-worker` (show, complete, block, comment, attach,
create and the like) is what workers get. `kanban` adds `kanban_list` and `kanban_unblock` for an
agent that organises the work; turn it on with `indus tools enable kanban` (`--platform` for a chat platform). When a chat on a
messaging platform creates a task, that chat is subscribed to it and hears when the task is done,
blocked or waiting for review. See [Tools and toolsets](../reference/tools.md) for every tool and
[`indus kanban`](../reference/cli.md#indus-kanban) for every option.
