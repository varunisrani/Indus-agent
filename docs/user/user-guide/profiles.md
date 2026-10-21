# Profiles

A profile is a complete, separate Indus Agent: its own settings, keys, conversations, memories,
skills, scheduled jobs and messaging gateway. Use one for work and one for personal projects, or
give a team bot its own. The first profile is called `default` and lives in the home folder itself
(`~/.indus`); others live under `~/.indus/profiles/<name>`.

<!-- test -->
```console
$ indus profile create work --no-alias
Created profile 'work' at ...
Copied ...
Use it with `indus -p work` or `indus profile use work`.
```

## Using a profile

| How | Effect |
|---|---|
| `indus -p work ...` | this one command runs as `work` (the flag works in any position) |
| `INDUS_PROFILE=work` | the same, from the environment |
| `indus profile use work` | `work` becomes the profile used when nothing says otherwise |
| an alias | `indus profile create` writes a small `work` command that starts `indus -p work`; `--no-alias` skips it |

`indus profile list` shows them all; the sticky profile is marked. `/whoami` in a chat says which
profile and home you are on.

## Creating from another profile

| Option | Copies |
|---|---|
| (none) | nothing: an empty profile with the bundled skills |
| `--clone` | settings, `.env`, persona, memories and skills |
| `--clone-all` | everything except history and runtime state |
| `--clone-channels` | also the messaging channel settings and tokens |
| `--clone-from NAME` | clone that profile instead of the current one |

`indus profile describe work --text "Client projects"` records what a profile is for, and
`indus profile show work` prints it with the profile's details. `rename` and `delete` do what they say (a deleted profile cannot be brought
back, so take an `indus backup` first if in doubt).

## What is shared

Nothing by default. Each profile's gateway is a separate process with its own platform tokens: one
bot token can serve only one profile at a time, and a second gateway that tries to use the same
token is refused. The desktop app and the dashboard show every profile of the machine and switch
between them.
