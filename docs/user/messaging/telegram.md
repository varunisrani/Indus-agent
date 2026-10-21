# Telegram

Indus Agent talks to Telegram as a bot, through the Bot API. No extra libraries are needed.

## What you need

A bot token. In Telegram, open a chat with **@BotFather**, send `/newbot`, choose a name and a
username, and copy the token it gives you.

## Setting it up

```bash
indus gateway setup telegram
```

or set `TELEGRAM_BOT_TOKEN` yourself (`indus config set TELEGRAM_BOT_TOKEN <token>`), then
`indus gateway start`. Write to your bot; it answers with a pairing code that you approve with
`indus pairing approve telegram <code>`. To skip pairing for yourself, put your numeric user id in
`INDUS_TELEGRAM_ALLOWED_USERS`.

## How it behaves

- The gateway fetches updates by long polling; nothing has to be reachable from the internet. A
  webhook can be configured instead (it always needs its secret).
- In groups the bot answers when mentioned or replied to; in forum groups each topic is its own
  conversation and replies stay in their topic.
- Replies are written into the message as they stream, and long ones are split at Telegram's
  4,096-character limit.
- Approvals and questions show as buttons. The bot's command menu lists the slash commands.
- Photos, voice notes and documents you send are passed to the agent (voice notes are transcribed
  when speech to text is set up).
