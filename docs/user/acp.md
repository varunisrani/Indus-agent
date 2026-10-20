# Indus Agent in your editor (ACP)

Editors that speak the Agent Client Protocol (Zed, and the ACP clients for VS Code and JetBrains)
can run Indus Agent as an external agent. The editor starts `indus acp` and talks to it over
stdin/stdout.

## Setting it up

1. Configure a provider once: `indus setup` (or `indus acp --setup`, which an editor can run for
   you from its sign-in screen).
2. Check the install: `indus acp --check` prints `OK` when the ACP support is installed.
3. Point your editor at the command `indus acp`. In Zed, for example, add an agent server whose
   command is `indus` with the argument `acp`.

By default `indus acp` is a viewer of your profile's gateway daemon (`indus gateway start`), so
the same conversation can be followed from a terminal and survives an editor restart. Without a
daemon, run `indus acp --local` to host the sessions inside the editor's process instead.

## What the editor gets

- Sessions you can open, load, list, fork and cancel; replies, reasoning, tool calls (edits as
  diffs, commands as the command line), plans and context usage stream into the editor.
- Permission cards for risky commands and file writes: Allow once, Allow for session, Allow always
  (when the command may be always allowed), Deny and Deny always. A card nobody answers expires
  after `approvals.timeout` seconds (300 by default); the agent reports that nobody answered,
  which is not a refusal.
- Questions from the agent as a form (when the editor supports form elicitation) or as a card
  with one button per choice plus Skip.
- Slash commands: `/help` lists them. `/branch` is your editor's fork action instead.
- One mode, "Default": Indus asks before edits. The model picker switches the model of the current
  session only; your profile default is left alone.

## Tools

Editor sessions use the `indus-acp` toolset: files, terminal and processes, web and browser,
memory, todo, session search, skills, code execution, delegation and vision. Messaging delivery,
scheduled jobs and the interactive question tool are not part of it. Change it with
`indus tools` (the `acp` platform); add `no_mcp` to the list to leave your configured MCP servers
out. MCP servers your editor provides are always added for that editor's sessions; their settings
(commands, environment, headers) are kept in memory only.

## Security note

Some editors can be set to answer permission requests automatically. With such a setting,
terminal commands and Python code run without anyone looking at them. If you use one, remove
`terminal` and `execute_code` from the ACP toolset (`indus tools`, platform `acp`) so the agent
cannot run commands unattended.
