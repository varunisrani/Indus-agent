# Matrix

Indus Agent works with any Matrix homeserver through a user account's access token.

## What you need

A Matrix account for the bot on your homeserver, and its **access token** (in Element: Settings,
Help & About, Access Token; or log in through the client-server API).

## Setting it up

```bash
indus gateway setup matrix
```

or set `MATRIX_HOMESERVER` (for example `https://matrix.example.org`) and `MATRIX_ACCESS_TOKEN`,
then `indus gateway start`. Invite the bot to a room, or start a direct chat with it.

## How it behaves

- Direct chats are private conversations; in rooms the bot answers when mentioned or replied to,
  and threads are kept as their own conversations.
- `!command` works in place of `/command`, because Matrix clients keep `/` for themselves.
- An allow-list of rooms limits where it listens; direct chats are always allowed.
- **Encrypted rooms** need the `matrix-e2ee` extra (Linux only; it builds libolm, so a C++
  compiler and cmake are needed). Without it the bot works in unencrypted rooms only.
