# Weixin

Indus Agent can answer from a personal WeChat (Weixin) account through Tencent's iLink bot
interface.

## What you need

An iLink bot **token** and the **account id** it belongs to.

## Setting it up

```bash
indus gateway setup weixin
```

or set `WEIXIN_TOKEN` and `WEIXIN_ACCOUNT_ID`, then `indus gateway start`.

## How it behaves

- Private chats are conversations. Group messages are kept only when the group policy allows them
  (`open`, or an allow-list naming the group); naming the bot in a group addresses it.
- The gateway resumes from where it left off after a restart, so messages are neither lost nor
  answered twice.
- Media are encrypted the way iLink expects.
