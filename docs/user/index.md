# Indus AGI Agent

Indus AGI Agent is an AI agent you run on your own machine. It talks to the model provider you
choose, runs commands and edits files for you (asking first when something is risky), remembers what
it learns about you and your projects, and turns work it has done well into skills it can reuse.

You can reach the same agent from several places:

| Where | Start it with | Page |
|---|---|---|
| A terminal, line by line | `indus` | [Classic CLI](user-guide/cli.md) |
| A terminal, full screen | `indus --tui` | [Terminal UI](user-guide/tui.md) |
| A desktop window | the Indus desktop app | [Desktop app](user-guide/desktop.md) |
| A browser | `indus dashboard` | [Web dashboard](user-guide/dashboard.md) |
| Your editor | `indus acp` | [Editors (ACP)](acp.md) |
| Chat apps (Telegram, Slack, Discord and more) | `indus gateway run` | [Messaging](messaging/index.md) |
| Scripts | `indus -z "..."` | [Classic CLI](user-guide/cli.md#one-shot-runs) |

Everything lives in one home folder, `~/.indus` by default (`INDUS_HOME` moves it): settings,
keys you chose to keep in a file, conversations, memories, skills, logs. Several
[profiles](user-guide/profiles.md) can live side by side, each with its own settings and history.

## Where to go next

- New here: [Install](getting-started/install.md), then the [Quickstart](getting-started/quickstart.md).
- Choosing what the agent may do: [Approvals and security](user-guide/security.md) and
  [Tools and backends](user-guide/tools-and-backends.md).
- Looking something up: the [Reference](reference/cli.md) pages are generated from the program
  itself, so they always match the version they ship with.
- Building on it: the [Developer guide](developer/architecture.md).

The documentation is also available as plain text for language models: `llms.txt` (an index) and
`llms-full.txt` (every page) at the root of this site.
