# DingTalk

Indus Agent connects to DingTalk as a robot in Stream Mode, so nothing has to be public.

## What you need

An internal app with a robot in the DingTalk developer console, using Stream Mode: its **Client
ID** (AppKey) and **Client Secret** (AppSecret).

## Setting it up

```bash
indus gateway setup dingtalk
```

or set `DINGTALK_CLIENT_ID` and `DINGTALK_CLIENT_SECRET`, then `indus gateway start`.

## How it behaves

- One-to-one chats and group chats work; in groups the bot answers when mentioned. Group chats can
  be limited with `INDUS_DINGTALK_ALLOWED_CHATS`.
- Replies go back through the conversation's own reply address, or the robot API when that has
  expired.
- The stream reconnects on its own after a drop.
