# QQ Bot

Indus Agent connects to QQ through the official QQ Bot API.

## What you need

A bot on the QQ open platform: its **App ID** and **client secret** (AppSecret).

## Setting it up

```bash
indus gateway setup qqbot
```

or set `QQ_APP_ID` and `QQ_CLIENT_SECRET`, then `indus gateway start`. The gateway opens QQ's
WebSocket gateway itself, so nothing has to be public.

## How it behaves

- Private (C2C) chats, group chats, guild channels and guild direct messages each work as
  conversations; in groups and channels the bot answers when mentioned.
- Replies are split at 4,000 characters.
