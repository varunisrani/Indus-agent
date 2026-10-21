# Yuanbao

Indus Agent can work as a bot on Tencent Yuanbao.

## What you need

A Yuanbao bot's **App ID** and **App Secret**.

## Setting it up

```bash
indus gateway setup yuanbao
```

or set `YUANBAO_APP_ID` and `YUANBAO_APP_SECRET`, then `indus gateway start`. The gateway keeps one
signed WebSocket to Yuanbao, so nothing has to be public.

## How it behaves

- If another connection takes the bot over, the gateway stops using it and says so.
- With the `yuanbao` toolset turned on, the agent gets Yuanbao's own tools as well.
