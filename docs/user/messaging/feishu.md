# Feishu / Lark

Indus Agent connects to Feishu (and Lark, its international version) as a custom app bot.

## What you need

A custom app in the Feishu or Lark developer console with the bot feature turned on: its **App
ID** and **App Secret**, and the event subscription for received messages.

## Setting it up

```bash
indus gateway setup feishu
```

or set `FEISHU_APP_ID` and `FEISHU_APP_SECRET`. By default the gateway opens Feishu's long
connection, so nothing has to be public; an event webhook is the alternative.

## How it behaves

- Direct chats and group chats work; in groups the bot answers when mentioned.
- With the `feishu` toolset turned on, the agent can also read Feishu documents and their comments
  (`feishu_doc_read` and related tools).
