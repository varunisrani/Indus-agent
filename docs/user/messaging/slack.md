# Slack

Indus Agent connects to Slack through Socket Mode, so no public web address is needed. The Slack
libraries come with the standard install.

## What you need

A Slack app in your workspace with:

- **Socket Mode** turned on, and an **app-level token** with the `connections:write` scope;
- a **bot token**, with scopes to read and write the conversations it should see (for example
  `app_mentions:read`, `chat:write`, `channels:history`, `im:history`, `files:read`);
- event subscriptions for `message.im`, `message.channels` and `app_mention`.

## Setting it up

```bash
indus gateway setup slack
```

or set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`, then `indus gateway start`. Approve yourself with
the pairing code from a direct message, or list member ids in `INDUS_SLACK_ALLOWED_USERS`.

## How it behaves

- Direct messages are private conversations. In a channel, a message that mentions the bot starts
  a thread, and the conversation continues inside that thread.
- One channel in two workspaces is two separate conversations.
- Replies are written in Slack's formatting; files you share are passed to the agent.
- Approvals and questions show as buttons.
