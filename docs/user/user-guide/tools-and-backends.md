# Tools and backends

## Tools and toolsets

The model works through tools: run a command, read or patch a file, search the web, open a page in
a browser, ask you a question, hand a task to a helper agent, schedule a job. Tools come in
*toolsets* (`terminal`, `file`, `web`, `browser`, `memory`, ...), and each surface starts from a
bundle: the classic CLI and TUI from `indus-cli`, Telegram from `indus-telegram`, and so on. Every
tool and toolset is listed in [Tools and toolsets](../reference/tools.md).

```bash
indus tools                      # a checklist on a terminal
indus tools list                 # what the CLI gets, and which tools are not ready here
indus tools disable browser      # leave the browser tools out of CLI sessions
indus tools enable image_gen --platform telegram
```

`indus tools list` also says why a tool is not ready (a missing key, a library that is not
installed). A tool that is not ready is left out of the session automatically, so the model never
sees a tool it cannot use. For one run, `-t terminal,file` picks toolsets explicitly.

Web search works with a Brave Search key (`BRAVE_SEARCH_API_KEY`), a SearXNG instance
(`web.searxng_url`) or keyless DuckDuckGo (installed with the standard install).

## Terminal backends

Commands the agent runs go to a *backend*, chosen with `terminal.backend`
(or `indus setup terminal`):

| Backend | Where commands run | Needs |
|---|---|---|
| `local` | this machine, in the session's folder | nothing (the default) |
| `docker` | a container from the sandbox image | Docker or Podman |
| `ssh` | another machine | `terminal.ssh_host` and an SSH key or agent |
| `singularity` | an Apptainer / Singularity container | `apptainer` or `singularity` |
| `modal` | a Modal sandbox in the cloud | the `modal` extra and a Modal token |
| `daytona` | a Daytona sandbox | the `daytona` extra and a Daytona key |
| `vercel_sandbox` | a Vercel Sandbox | the `vercel` extra and a Vercel token |

Exported variables and the current folder carry over from one command to the next
(`terminal.persistent_shell`). A command that runs longer than `terminal.timeout` (180 s) is stopped;
long jobs can run in the background, and the agent checks on them with its process tool. Commands
get a clean environment: your provider keys are never passed to them, and only the names listed in
`terminal.env_passthrough` are added.

### Docker

The default image is Indus Agent's own sandbox image (`ghcr.io/indusagi/indus-agent-sandbox`). The
container is kept between commands and, by default, between runs (`terminal.container_persistent`),
with limits on CPU, memory and disk (`terminal.container_cpu`, `container_memory_mb`,
`container_disk_mb`). Your project folder is mounted at `/workspace` only when you ask for it
(`terminal.docker_mount_cwd_to_workspace`); `terminal.docker_network: false` runs the container
offline. Files the agent writes inside a sandbox can be copied back when the sandbox is cleaned up.

### Cloud sandboxes

Modal, Daytona and Vercel sandboxes start on demand and are cleaned up after
`terminal.lifetime_s` (300 s) without use. Their SDKs are optional extras; credentials are read from
the secret store or the vendor's usual environment variables. `terminal.credential_files` copies
chosen files (for example a `.netrc`) into every sandbox, read-only.

## Helper agents and code

- `delegate_task` hands work to subagents that run with a subset of the tools
  (`delegation.child_toolsets`) and report back a summary; `/agents` shows them.
- `execute_code` runs a Python script that can call the other tools from code, which saves many
  round trips for loops over files or results (`code_execution.*`).
- `/moa` turns on mixture of agents for hard questions: several models answer and one combines them.
