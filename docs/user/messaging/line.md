# LINE

Indus Agent connects to LINE through a Messaging API channel.

## What you need

In the LINE Developers console, a Messaging API channel: its **channel access token** and its
**channel secret**.

## Setting it up

```bash
indus gateway setup line
```

or set `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET`. LINE delivers messages to a webhook:
the gateway listens on port 8646, path `/line/webhook` (loopback by default). Publish it through
your HTTPS reverse proxy or tunnel, enter that URL in the channel's webhook settings and set it in
`platforms.line.webhook_public_url`.

## How it behaves

- Every request is checked against the channel secret before it is read.
- Replies use LINE's free reply token when they are quick and fall back to push messages (which
  LINE meters) when they are not. For long tasks the bot can send a "still working" card first.
- Approvals and questions show as button cards.
- Groups and rooms are group conversations; mention the bot to address it.
