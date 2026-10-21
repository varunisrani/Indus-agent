# The protocol

Surfaces talk to the agent host with JSON-RPC 2.0 messages over stdio (the terminal UI bridge, the
editor adapter) or a WebSocket (the daemon, the desktop app, the dashboard). The Python models in
`indus.protocol` are the single source; `python -m indus.protocol.codegen` generates the
TypeScript types and catalogue in `packages/protocol`, and CI fails when they are stale.

Every method, event and error code is listed in the generated [Protocol](../reference/protocol.md)
reference.

## The shape of a session

1. `session.create` or `session.resume` returns a session id.
2. `prompt.submit` starts a turn. When the session is busy the request says what to do
   (`when_busy`): queue it, steer the running turn, or interrupt and redirect.
3. The host streams **events** for the session: `turn.started`, text and reasoning deltas, tool
   started, progress and completed, status, and `turn.done` with how the turn ended.
4. When the agent needs the person, the host sends a **server request** (approval, clarify, sudo,
   secret). Any attached client may answer with `server_request.answer`; the first answer wins and
   the others see it settled.
5. `session.interrupt` stops a turn; `session.steer` adds guidance to it.

## Clients and capabilities

A client announces what it can do with `client.capabilities` (for example that it can answer
server requests, and which surface it is). A client that cannot answer is never sent a question.

## Errors

Error codes have one meaning across every method, grouped (invalid request, not found, conflict,
unavailable, ...) so a client can handle a group without knowing every code. Codes that never
answer a method call (sent in a WebSocket close, for example) are listed separately.

## Attaching to the daemon

Clients attach to a profile's daemon over a WebSocket with a single-use ticket (`indus gateway
grant` prints one for the terminal UI). Several clients can view one session at once; events carry
a generation so a client that reconnects can replay what it missed.
