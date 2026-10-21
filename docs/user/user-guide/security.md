# Approvals and security

Indus Agent runs real commands on real machines. This page explains what it does on its own, what it
asks you first, and what it never does.

## Approvals

Before running a command that could change or delete something, the agent asks. You answer with
**allow once**, **allow for this session**, **always allow** (for commands where that is
offered) or **deny**. How it asks depends on `approvals.mode`:

| Mode | Behaviour |
|---|---|
| `manual` (default) | every risky command waits for your answer |
| `smart` | a second model looks at the risky command first and only asks you when it is unsure; `approvals.smart_policy` adds rules in plain words |
| `off` | never asks (the floors below still apply) |

`--yolo` on the command line, `/yolo` in a chat or Shift+Tab in the terminal UI turn asking off for
one session. A prompt nobody answers expires after `approvals.timeout` seconds (300); the agent is
told that nobody answered, which is not the same as a refusal.

Where nobody can answer, risky commands are refused by default: scheduled jobs
(`approvals.cron_mode`), one-shot runs (`approvals.single_query_mode`) and unattended entry points
such as the API server and webhooks (`approvals.unattended_mode`). Set one to `approve` only when
you trust everything that can reach it.

### Floors that always hold

Some commands are refused whatever the mode, `--yolo` included: deleting the root of the file
system, writing to raw disks, fork bombs and similar. Your own `approvals.deny` patterns join them.
`command_allowlist` does the opposite for commands you never want to be asked about.

You can check what the rules would do with a command without running it:

<!-- test -->
```console
$ indus approvals test "rm -rf /"
Command: rm -rf /
Backend: local
Verdict: deny (exit 3)
Why:     a safety floor blocks it whatever the approval settings
...
```

## Files the agent treats with care

- Writes to instruction files (`AGENTS.md` and the other rules files the agent reads, plus any
  globs in `security.protected_instruction_files`) always ask first.
- Indus Agent's own secret and control files, `.env` files, SSH and cloud credentials and key files
  are refused to the file tools and to `@file:` references.
- `--checkpoints` lets you roll file changes back (see [Sessions](sessions.md#checkpoints)).

## Secrets

- Keys live in the operating system's keychain when there is one; the settings file holds names,
  never values (`${NAME}` refers to a stored secret).
- Logs, tool output, exports and diagnostics mask anything that looks like a secret
  (`security.redact_secrets`).
- Commands the agent runs get an environment without your provider keys.

## Network

The web tools refuse private and local addresses (`security.allow_private_urls` lets LAN addresses
through; cloud metadata addresses stay refused) and anything on your block list
(`security.website_blocklist`). MCP packages started with `npx`, `uvx` and similar are checked
against the OSV malware database first (`mcp.osv_preflight`).

## Who can talk to the agent

- **Messaging**: a person the gateway does not know gets a pairing code (or nothing, see
  `gateway.unauthorized_dm_behavior`) and no access until you run `indus pairing approve`.
  Allow-lists per platform (`INDUS_<PLATFORM>_ALLOWED_USERS`) admit people up front. See
  [Messaging](../messaging/index.md#who-may-talk-to-the-bot).
- **Webhooks** run with a restricted toolset (web search, reading pages, looking at images, asking
  a question) and every request must carry a valid signature.
- **API server**: refuses requests without its key, also on loopback (`indus gateway api-key`).
- **Dashboard**: any address other than loopback needs a login (see [Web dashboard](dashboard.md)).

## Plugins, MCP servers and hooks

Plugins only load when you enable them (`indus plugins enable`). Shell hooks are each approved once
before they first run. Skills and plugins installed from a hub are scanned first, and a scan that
finds something dangerous blocks the install. See [Plugins and MCP](plugins-and-mcp.md).

## Reporting a problem

`indus doctor` lists security advisories that apply to your setup; `indus doctor --ack <id>` marks
one as read.
