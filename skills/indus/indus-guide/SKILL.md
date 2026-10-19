---
name: indus-guide
description: Use, configure and extend Indus Agent itself
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: indus
    tags: [indus, help, configuration, setup, profiles]
---

# Indus Agent guide

How to answer questions about Indus Agent and help the user set it up, configure it and extend it.
The command map and file layout are in `references/commands-and-files.md`; check details against
the installed version (`indus --help`, `indus <command> --help`) before you state them, because
commands and settings grow between releases.

## When to use

- The user asks how to do something *with Indus Agent*: switch model, add a key, change a setting,
  create a profile, find old sessions, schedule a job, add a skill.
- Something about the agent itself misbehaves (no model reply, a tool missing, a key not found).
- The user wants to extend the agent: new skills, skill bundles, tools, MCP servers, plugins.

## Procedure

1. **Find out where they are.** Which profile (`indus profile show`), which model
   (`indus status`), which surface (terminal chat, a messaging channel, the desktop app).
2. **Diagnose before advising.** For "it does not work" questions run `indus doctor` through
   `terminal` and read its findings; `indus logs` shows recent errors. Quote the relevant line to
   the user rather than guessing.
3. **Answer with the exact command or setting.** Prefer a CLI command the user can run over
   hand-editing files:
   - model and provider: `indus model` (interactive picker) or `indus config set model.default …`;
   - keys and sign-ins: `indus auth`, or the profile's `.env` (`indus config env-path`);
   - any setting: `indus config show`, `indus config get <key>`, `indus config set <key> <value>`,
     `indus config edit`;
   - tools: `indus tools list`, `indus tools enable|disable <toolset>`;
   - in a chat, `/help` lists slash commands; `/model`, `/new`, `/resume`, `/compress` and
     `/usage` are the common ones.
4. **Profiles keep things apart.** Each profile has its own settings, keys, memory, skills and
   history. `indus profile create <name>` makes one (`--clone` copies the current setup,
   `--no-skills` leaves out the bundled skills); `indus -p <name>` runs one.
5. **Memory and skills.** The agent keeps two small notes files (its own notes and a profile of
   the user) and writes them with the `memory` tool; changes show up in the next session. Skills
   live under the profile's `skills/` folder; `/skills` lists them, `/<skill-name>` runs one, and
   the `skill-authoring` skill explains how to write one.
6. **Extending.** New skills: `skill_manage` or a folder with a `SKILL.md`. Several skills as one
   command: a bundle file under `skill-bundles/`. External tools: MCP servers and plugins, each
   configured in `config.yaml`; check `indus <command> --help` for the commands your version has.
7. **Make changes only with consent.** Show the command or the config change first; run it after
   the user agrees. Secrets go through `indus auth` or the `.env` file, never into chat text.

## Pitfalls

- Stating a command or setting from memory that this version does not have. Check `--help`.
- Editing `config.yaml` by hand while a session is running: some settings are read once at
  start. Tell the user when a restart or `/new` is needed.
- Mixing up profiles: a key added in one profile is not visible in another.
- Printing API keys or tokens while debugging. Use `indus config show`, which masks them.
- Treating a memory write as visible in the current conversation; it appears from the next one.

## Verification

- The user's goal works: the model replies, the tool appears in `indus tools list`, the new skill
  appears in `/skills`, the setting reads back with `indus config get`.
- `indus doctor` reports no new problems after the change.
