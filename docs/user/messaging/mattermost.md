# Mattermost

Indus Agent connects to Mattermost with a bot or personal access token: the REST API for sending
and a WebSocket for receiving.

## What you need

A bot account (System Console, Integrations, Bot Accounts) and its token, and your server's URL.

## Setting it up

```bash
indus gateway setup mattermost
```

or set `MATTERMOST_URL` and `MATTERMOST_TOKEN`, then `indus gateway start`.

## How it behaves

- Direct messages are private conversations; in channels the bot answers when mentioned, and
  replies thread under the post. An allow-list of channels can limit where it listens.
- Type a space before a slash command (` /new`), because Mattermost keeps `/` for its own commands.
- `@channel`, `@all` and `@here` in replies are defused, so the bot never pings a whole channel.
- Replies are split at 4,000 characters.
