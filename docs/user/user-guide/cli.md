# Classic CLI

`indus` with no command starts a chat in your terminal: a prompt at the bottom, replies streamed
above it. This is the default interface; set `display.interface: tui` (or pass `--tui`) to make
the [full-screen terminal UI](tui.md) the default instead.

## Chatting

- **Enter** sends. A line ending in `\`, **Alt+Enter** or **Ctrl+J** starts a new line instead.
- **Ctrl+G** opens the draft in your `$EDITOR`.
- **Ctrl+C** clears a draft; with an empty prompt it stops the running reply; pressed twice
  within two seconds on an idle, empty prompt it leaves.
- **Ctrl+D** on an empty prompt leaves.
- `/help` lists the [slash commands](../reference/slash-commands.md); `/help <word>` filters them.

Useful commands while you chat:

| Command | What it does |
|---|---|
| `/new [title]` | start a fresh conversation |
| `/retry`, `/undo [N]` | send your last message again; take back the last N exchanges |
| `/stop` | stop the reply and this session's background processes |
| `/steer <text>` | add guidance to the running reply after its next tool call |
| `/model [name]` | show or switch the model for this conversation (`--global` also saves it) |
| `/compress` | summarise older turns to free context |
| `/usage`, `/context` | token use and cost; how full the context window is |
| `/branch` | continue a copy of this conversation in a new session |
| `/save md file.md` | save the conversation as Markdown, HTML or JSON |

## Pointing at things

Mention files and other context with `@`: `@file:src/app.py`, `@file:src/app.py:10-40` (a line
range), `@folder:docs`, `@diff` (your uncommitted changes), `@git:HEAD~1` (a commit),
`@url:https://example.com`. The content is put into your message. Files that hold credentials
(`.env` files, SSH keys, cloud credentials) are refused with a note.

A line starting with `!` runs a shell command yourself, without the model: `!git status`. It runs
on this machine in the session's folder, goes through the same approval rules as the agent's own
commands, and never enters the conversation. It is stopped after two minutes; Ctrl+C stops it
sooner.

## Starting options

| Option | What it does |
|---|---|
| `-m MODEL`, `--provider NAME` | the model and provider for this run |
| `-c [NAME]` | continue the newest conversation, or the one called NAME |
| `-r ID_OR_TITLE` | resume a saved conversation |
| `--in DIR` | work in another folder |
| `-w [NAME]` | start in a new git worktree of this repository (see [Sessions](sessions.md)) |
| `--checkpoints` | snapshot the folder before file changes, so `/rollback` can undo them |
| `-t LIST` | only these toolsets for this run |
| `-s SKILL` | load a skill into the conversation from the start |
| `--yolo` | run risky commands without asking (the safety floors still apply) |
| `--safe-mode` | start without your settings, rules files, memory or plugins, to troubleshoot |

Every option is listed in [Command line](../reference/cli.md#indus).

## One-shot runs

`indus -z "prompt"` answers one prompt and prints only the final reply, which suits scripts and
pipes. The exit code says how it went (0 for a reply). `--usage-file report.json` writes the
tokens and cost of the run. Nobody can answer an approval prompt in a one-shot run, so risky
commands are refused unless `approvals.single_query_mode` is `approve`.

## Settings from the command line

`indus config` reads and changes settings without opening the file:

<!-- test -->
```console
$ indus config get agent.max_turns
150
$ indus config set agent.max_turns 60
Set agent.max_turns = 60 in ...
$ indus config get agent.max_turns
60
```

`indus config set` puts each value where it belongs: credentials go to the keychain,
`UPPER_SNAKE_CASE` names to the profile's `.env` file, everything else to `config.yaml`. All keys
are listed in [Configuration keys](../reference/configuration.md).

## Quick commands

`quick_commands` in `config.yaml` adds your own slash commands: `{type: exec, command: ...}` runs a
shell command, `{type: alias, target: ...}` stands for another command. For example:

```yaml
quick_commands:
  gs:
    type: exec
    command: git status --short
```
