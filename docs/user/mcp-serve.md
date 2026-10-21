# Your chats as an MCP server (`indus mcp serve`)

`indus mcp serve` lets another AI client — any program that speaks the Model Context Protocol, such
as a desktop chat app or a coding assistant — read and answer the messaging conversations of one
Indus Agent profile: the Telegram, Discord, Slack, WhatsApp, Signal, Matrix and other chats its
gateway is connected to.

The client starts the command as a child process and talks MCP over its stdin and stdout. Nothing
listens on the network, and the client needs no key or token of its own: the server reads the
profile's own session store and reaches the profile's gateway daemon through its owner-only local
control socket, so it only works for the user who owns the profile.

## What it offers

Always listed (eight tools):

| Tool | What it does |
|---|---|
| `conversations_list` | The profile's messaging conversations, newest first, with the session key the other tools take. Filters: `platform`, `search`; `limit` 1–200 (default 50). |
| `conversation_get` | One conversation's details: chat and thread ids, times, token totals. |
| `messages_read` | The latest user and assistant messages of a conversation, oldest first (`limit` 1–200, default 50; each text cut at 2,000 characters). |
| `attachments_fetch` | The images and files one message carries. Files outside the folders media may be sent from are listed as refused, never read. |
| `events_poll` | New messages (and approval prompts, below) since a cursor. Keep the returned `next_cursor` for the next call. |
| `events_wait` | The same, as a long poll: waits up to `timeout_ms` (default 30 s, at most 5 minutes) for the next event. |
| `messages_send` | Sends a text through the running gateway to a target such as `telegram:123456789` or `discord:#general`. Text only. |
| `channels_list` | The chats you can send to, each with the target string `messages_send` takes. |

Listed only while the profile's gateway daemon runs and offers its approval queue:

| Tool | What it does |
|---|---|
| `permissions_list_open` | The approval prompts waiting for an answer in any session the daemon hosts (chats, and terminal or editor sessions attached to it), with the command (secrets masked), why it needs approval and the answers allowed. |
| `permissions_respond` | Answers one prompt with `allow-once`, `allow-session`, `allow-always` or `deny`. The first answer from anywhere wins; a later one is told the prompt is already settled. |

Reading never changes anything in the profile. Every tool answers JSON; a problem (a bad argument,
no session store yet, no daemon) comes back as an object with an `error` field rather than a failed
call.

## What needs the daemon

Start the gateway first (`indus gateway start`, or keep `indus gateway run` running):

- `messages_send` goes through the daemon. Without it the tool answers
  `{"error": "daemon not running"}`; the read tools keep working from the store.
- The two approval tools exist only when the daemon was reachable **when the server started**. The
  tool list is fixed for the life of the server: if you start the daemon later, restart the MCP
  server (most clients have a reload button) to get them.

## Setting it up in a client

Most clients take a JSON block naming the command. The smallest one, for the default profile:

```json
{
  "mcpServers": {
    "indus": {
      "command": "indus",
      "args": ["mcp", "serve"]
    }
  }
}
```

For another profile, name it before the command (`-p` works in any position):

```json
{
  "mcpServers": {
    "indus-work": {
      "command": "indus",
      "args": ["-p", "work", "mcp", "serve"]
    }
  }
}
```

If the client does not find `indus` on its `PATH`, give the full path of the command
(`which indus` prints it). No `env` block is needed: the platform credentials stay in the
profile, and none of them belongs in the client's configuration.

To check the command by hand, run `indus mcp serve` in a terminal: it waits silently for MCP frames
on stdin (press Ctrl-D to end it). Add `--verbose` to see debug output on stderr; stdout always
carries MCP frames only, so a client never reads a stray log line.

## Good to know

- The server shows the profile's own conversations only; one server serves one profile.
- Sending is text only; media and edits are not offered.
- A client that answers approval prompts can let commands run on your machine. Only connect clients
  you trust, as you would only give a person you trust the keys to your chat accounts.
- Without the MCP support installed, the command prints how to install it and exits 1.
