# Messaging

The messaging gateway connects your Indus Agent to chat apps, so you can talk to it from your phone,
a team channel or an email inbox. One gateway per profile serves every platform you enable; it also
runs your [scheduled jobs](../user-guide/cron.md) and is the daemon the terminal UI, the desktop app
and editors attach to.

## Setting up a platform

```bash
indus gateway setup telegram     # or run it without a name for a menu
```

The setup asks for the platform's credentials (stored in the keychain), who may talk to the bot,
and checks the connection. You can also set the credential yourself:
`indus config set TELEGRAM_BOT_TOKEN <token>` or a line in the profile's `.env` file. A platform
starts when its credentials are present, unless `platforms.<name>.enabled` is `false`.

Every platform with its credential names is listed in
[Messaging platforms](../reference/messaging-platforms.md). The standard install already includes
the libraries all of them need, except end-to-end encrypted Matrix and Discord voice.

## Running the gateway

| Command | What it does |
|---|---|
| `indus gateway run` | run in the foreground (Ctrl+C stops it) |
| `indus gateway start` / `stop` / `restart` | run it in the background |
| `indus gateway status` | is it running, and how is each platform |
| `indus gateway install --start` | install it as a user service (systemd on Linux, launchd on macOS) so it starts with your login |

Stopping lets running replies finish or hands them over to the next start; nothing a person sent is
lost across a restart, and a reply that was being sent when the gateway stopped is delivered after
it comes back.

## Who may talk to the bot

Nobody, until you say so. A person the gateway does not know who writes to the bot privately gets a
short pairing code (`gateway.unauthorized_dm_behavior`: `pair`, `ignore` or `decline`). You let them
in from your terminal:

<!-- test -->
```console
$ indus pairing list
Nobody has asked to pair yet, and nobody is paired.
```

```bash
indus pairing approve telegram 4KQ7ZT
indus pairing revoke telegram 123456789
```

To admit people up front, list their ids: `INDUS_TELEGRAM_ALLOWED_USERS=123456789,987654321` (each
platform has its own `INDUS_<PLATFORM>_ALLOWED_USERS`). `INDUS_<PLATFORM>_ALLOW_ALL_USERS=true` opens
a platform to everyone who can reach the bot; use it only for bots in closed spaces. On broadcast
surfaces such as ntfy topics, pairing codes are never sent.

In group chats the bot answers when it is mentioned or replied to, and each person gets their own
conversation (`gateway.group_sessions_per_user`). Messages from other bots are ignored unless
`INDUS_<PLATFORM>_ALLOW_BOTS` allows them, and a loop guard stops two bots from talking to each
other forever.

## In the chat

- Slash commands work in chats too (`/help`; some platforms use `!` or a space before the `/`
  because their clients keep `/` for themselves). `/new` starts a fresh conversation.
- Risky commands ask for approval with buttons where the platform has them, or with `/approve` and
  `/deny`.
- `/sethome` makes the current chat the home channel for scheduled results and notices.
- `/bg <prompt>` runs a task in the background and posts the result here.
- Voice messages are transcribed when speech to text is set up (`gateway.stt_enabled`).
- Replies are edited in place as they are written on platforms that allow it
  (`platforms.<name>.streaming`), and tool activity is shown as configured
  (`platforms.<name>.tool_progress`).

## Sending from scripts

`indus send` sends a message to a chat without a model; it connects to the platform just for the
send, so it works whether or not the gateway is running:

```bash
indus send telegram "Backup finished"           # the platform's home channel
indus send telegram:123456789 "Build is green"  # one chat
indus send discord:#releases "v1.2 is out"      # a channel by name
indus send all "Maintenance at 18:00"           # every home channel
```

The text can also come on stdin; `--file PATH` attaches a file from a folder media may be sent
from.

## Settings that apply to every platform

`platforms.<name>.home_channel`, `reply_to_mode`, `typing_indicator`, `streaming`, `tool_progress`
and `channel_overrides` (a model, provider or extra system prompt per channel) exist for each
platform; `gateway.*` holds the rest. See [Configuration keys](../reference/configuration.md#platforms).
