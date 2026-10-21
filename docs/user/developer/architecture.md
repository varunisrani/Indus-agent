# Architecture

Indus Agent is one Python package, `indus`, plus TypeScript front ends (the terminal UI, the desktop
app and the web dashboard) that talk to it over a JSON-RPC protocol.

## Layers

The package is split into layers; a module may import only from layers below its own, and CI
enforces it (`uv run lint-imports`). From the top:

| Layer | Packages | What lives there |
|---|---|---|
| Surfaces | `indus.cli`, `indus.tui_bridge`, `indus.gateway`, `indus.web`, `indus.acp`, `indus.mcp_serve` | the ways in: command line, terminal UI bridge, messaging gateway and daemon, dashboard and headless backend, editor protocol, MCP server |
| Research | `indus.research` | batch runs and trajectory tools |
| Service | `indus.service` | sessions, the slash-command registry, the session host the surfaces share, RPC dispatch |
| Learning and extensions | `indus.learning`, `indus.plugins`, `indus.mcp`, `indus.cron` | memory, skills, curator; plugins and hooks; the MCP client; scheduled jobs |
| Core | `indus.core` | the agent loop: prompt building, model calls, tool rounds, compression, interrupts |
| Tools | `indus.tools` | the tool registry, toolsets, dispatch and the built-in tools |
| Backends | `indus.backends` | where commands run: local, Docker, SSH and cloud sandboxes |
| Providers and safety | `indus.providers`, `indus.safety` | model providers and transports; approvals, redaction, URL and path policies |
| Protocol and state | `indus.protocol`, `indus.state` | the wire models; the SQLite session store |
| Foundation and platform | `indus.foundation`, `indus.platform` | config, home layout, scope, secrets, clock, logging; facts about the host |

## A turn

1. A surface submits the person's message to a session (`prompt.submit`).
2. The service saves the message, then the agent loop builds the prompt (persona, rules files,
   memory, skills index, tool schemas) and calls the model.
3. Tool calls are checked (approvals, path and URL policies), run through the registry, and their
   results saved; the loop calls the model again until it answers without tools or a limit is hit.
4. Events stream to every viewer of the session as they happen: text and reasoning deltas, tool
   start and end, status, the final message. Questions for the person (approvals, clarifications,
   secrets) travel as server requests that any attached surface may answer; the first answer wins.

## Profiles and scope

One process can serve several profiles. Code never reads the home folder, environment, config or
secrets from process globals: it reads them from the current `Scope` (`indus.foundation.scope`),
which carries the profile through tasks and threads. Work started without a scope fails loudly
instead of landing in the wrong profile.

## Processes

- `indus` alone hosts the agent in its own process (`--local`) or attaches to the profile's
  **gateway daemon**, which owns the sessions, the messaging platforms and the scheduler.
- The terminal UI is a TypeScript program that speaks the protocol to the daemon (or to a bridge
  process it starts).
- The desktop app finds or starts a backend and connects to it over a WebSocket with single-use
  tickets; the dashboard is served by the same web backend.

## Determinism in tests

Time comes from an injected `Clock`, so tests never sleep; the fake model in `tests/fakes/model`
replays scripted streams in every wire format the providers use.
