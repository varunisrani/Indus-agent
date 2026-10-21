# Discord

Indus Agent joins your Discord server as a bot. The `discord` library comes with the standard install.

## What you need

1. In the Discord Developer Portal, create an application and add a **Bot** to it.
2. Under the bot's settings, turn on the **Message Content** intent.
3. Copy the bot token, and invite the bot to your server with the `bot` and
   `applications.commands` scopes.

## Setting it up

```bash
indus gateway setup discord
```

or set `DISCORD_BOT_TOKEN`, then `indus gateway start`. Approve yourself with the pairing code the
bot sends you in a direct message, or list user ids in `INDUS_DISCORD_ALLOWED_USERS`.

## How it behaves

- Direct messages are private conversations. In a server channel the bot answers when mentioned and
  opens a thread for the conversation, bringing the channel's recent messages along as context.
- Slash commands are registered as Discord application commands.
- Replies are split at Discord's 2,000-character limit; they do not mention anyone unless configured.
- Approvals and questions show as buttons; with a role allow-list, only people with a granted role
  can press them.
- **Voice channels**: with the `discord-voice` extra (and libopus and ffmpeg on the system),
  `/voice join` in a server lets the bot listen and answer in a voice channel.
