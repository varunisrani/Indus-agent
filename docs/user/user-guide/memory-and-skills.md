# Memory and skills

Indus Agent gets better the longer you use it in two ways: it keeps short **memories** that every
later conversation sees, and it writes **skills**, tested procedures it can load when a similar task
comes up.

## Memory

Two small stores live in the profile's `memories/` folder:

| Store | Holds | Size |
|---|---|---|
| `MEMORY.md` | the agent's own notes: facts about your projects and environment | `memory.notes_char_limit` (2,200 characters) |
| `USER.md` | what it learned about you: preferences, habits, how you like answers | `memory.profile_char_limit` (1,375 characters) |

The agent adds to them with its `memory` tool when you say "remember that ..." and on its own during
background reviews (every `memory.review_interval` turns). Both stores are loaded into the start of
each new conversation; they are kept small on purpose so they never crowd out the work.

<!-- test -->
```console
$ indus memory show
saved notes (MEMORY.md): 0% — 0/2,200 chars
  (empty)
user profile (USER.md): 0% — 0/1,375 chars
  (empty)
```

`indus memory edit` opens both stores in your editor, `indus memory status` shows where they live,
and `indus memory off` stops using memory in this profile.

With `memory.write_approval: true` every change waits for you: the CLI asks, and elsewhere changes
are staged for `indus memory pending` / `approve` / `reject`. `memory.provider` adds one external
memory service beside the built-in stores.

## Skills

A skill is a folder with a `SKILL.md` file: a short description the agent sees all the time, and
instructions it loads only when the skill fits the task. Indus Agent ships with
[starter skills](../reference/skills.md) and copies them into each new profile.

```bash
indus skills list          # what this profile has
indus skills view code-review
```

In a chat, `/skills` lists them and every skill can be run as its own slash command
(`/code-review`). `-s NAME` on the command line, or `skills.auto_load`, loads a skill in full from
the start. `indus bundles` groups several skills under one command.

### Skills the agent writes

After a busy task, a background review looks at what happened and may save the way it solved the
problem as a new skill or improve an existing one (`skills.review_interval`, `learning.review.*`).
`/learn <description>` asks for a skill directly; `/refine` runs a review now. With
`skills.write_approval: true` each change waits as a pending change for you to approve.

The curator looks after these agent-made skills: unused ones become stale after
`curator.stale_after_days` and are archived after `curator.archive_after_days`; `indus curator`
lists, pins, restores and prunes them. `indus journey` shows what the agent has learned over time.

### Writing your own

```text
~/.indus/skills/deploy-preview/SKILL.md
```

```markdown
---
name: deploy-preview
description: Build the site and publish a preview link for the current branch
---

# Deploy a preview

1. Run `make build` and stop if it fails.
2. ...
```

Skills inside a repository (`.indus/skills/`, `.agents/skills/`) load only for repositories you
trust (`indus skills trust-project`). `skills.external_dirs` adds more folders.

### Installing from a hub

`indus skills search <words>`, `indus skills inspect <name>` and `indus skills install <name>` find
and install shared skills from configured sources. Every skill is scanned before it is installed,
and a skill flagged as dangerous is refused.
