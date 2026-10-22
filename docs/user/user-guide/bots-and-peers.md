# Bots and peers

Each profile can act as a bot: a named agent with its own settings, memory and keys. Bots on the
same machine can share a [group room](rooms.md), and bots on other machines are reached through
peers: other Indus gateways you register by URL and key.

## The bot chat

Every profile has one long-running conversation of its own, titled `Bot chat`. It is hidden from the
session list and found again by its title, so it stays the same conversation even after it is
compressed. Messages that arrive "for the bot" rather than in a particular session land here:

- a peer's [`indus peer dm`](#messaging-a-peer) runs one turn in it;
- a scheduled job with `--deliver self-chat` adds its result to it as an incoming message (no turn
  runs), so the bot sees it the next time you talk to it there.

### Bots that message other bots

In its bot chat, a bot can message other bots itself: turns there get the `message_agent` tool.
Its target is a profile on this machine (`alice`) or a bot on a [peer](#peers) (`office`, or
`office/research` for a named profile there); a local profile name always wins over a peer of the
same name. The message lands in the other bot's own bot chat, signed with the sender's name, and
the reply comes back to the asking bot marked as outside text, so instructions inside it are not
followed blindly.

A bot-chat turn uses the tools you saved for the `bots` platform with `indus tools`, or else the
`indus-bots` bundle: the usual core tools plus `message_agent`. Only bot chats get this tool, and
only when the profile's gateway is running (`indus gateway start`): the gateway's chat and its API
server (which a peer's `indus peer dm` reaches) run them as bot turns. A conversation run without
the gateway (the classic CLI, the terminal UI, an editor over ACP) has no `message_agent`, and
webhook turns never get it.

The desktop app's **Bots** page lists the profiles as bots (with their scheduled jobs), lets you
hide one or give it a new picture, and shows the [group rooms](rooms.md).

## Peers

A peer is another Indus gateway whose [API server](../messaging/api-and-webhooks.md#api-server) you
may call. On the other machine, turn the API server on and make a key for you:

```bash
indus config set gateway.api_server.enabled true
indus gateway api-key create --label peer-laptop      # prints the key once
indus gateway restart
```

Then register it here:

```bash
indus peer add office --url https://office.example.com --note "office desktop"
# asks for the key (hidden); or pipe it in with --key-stdin
indus peer list
```

The key is never a command-line argument. It is stored in this profile's secret store under
`INDUS_PEER_<NAME>_KEY`; for a headless setup you can put it in the profile's environment under the
same name. `indus peer list` shows the peers (name, URL, note) and whether each has a key; the peer
list itself never holds a key. `indus peer remove <name>` forgets a peer and its stored key.

Requests to a peer refuse private and loopback addresses, and never follow a redirect. For a peer on
your own network, add it with `--lan`; that allows its private address. A URL with a user name or
password in it is refused.

## Messaging a peer

```bash
indus peer dm office "What changed in the build today?"     # one turn, prints the reply
indus peer dm office/research "Summarise the open issues"   # a named profile on the peer
echo "a long question" | indus peer dm office -              # the message from stdin
```

The target is `peer` (the peer's default profile) or `peer/profile`. `dm` runs one turn in that
bot's [bot chat](#the-bot-chat) on the peer, waits up to 10 minutes and prints the answer.

For longer work, `indus peer run <target> "message"` starts the turn and prints a run id at once;
`indus peer status <target> <run-id>` shows how it is going (and the output when it finished), and
`indus peer stop <target> <run-id>` stops only that run. `--json` prints machine-readable output.

Exit codes: `0` done, `1` a peer or delivery problem (the message says what to do), `2` a usage
error. If the request went out but no answer came back in time, the turn is still running on the
peer: do not send it again.

See [`indus peer`](../reference/cli.md#indus-peer) for every option.
