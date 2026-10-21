# IRC

Indus Agent joins IRC networks as a regular client.

## What you need

A server and a nickname; optionally an account for SASL or NickServ.

## Setting it up

```bash
indus gateway setup irc
```

or set `IRC_SERVER` and `IRC_NICKNAME` (and the channels to join in `platforms.irc`), then
`indus gateway start`.

## How it behaves

- TLS is used by default, and passwords are never sent without it unless you allow it explicitly.
- Private messages to the bot's nickname are conversations; in channels the bot answers when
  addressed by name (`indus: ...`).
- Replies are plain text, one line per line, each at most 450 bytes, sent at a pace the network
  accepts.
- A taken nickname gets `_` appended; a lost connection is reopened.
