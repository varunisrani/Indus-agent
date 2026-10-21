# Microsoft Teams

Indus Agent works as a Teams bot through the Bot Framework.

## What you need

An Azure Bot registration (or a Teams app with a bot) with its **app (client) id**, a **client
secret** and the **tenant id**. The bot's messaging endpoint must be a public HTTPS URL that
reaches the gateway.

## Setting it up

```bash
indus gateway setup teams
```

or set `TEAMS_CLIENT_ID`, `TEAMS_CLIENT_SECRET` and `TEAMS_TENANT_ID`. The gateway listens for the
Bot Connector on port 3978, path `/api/messages`, on loopback unless you open it
(`platforms.teams.webhook_public`); put it behind your HTTPS reverse proxy and set that URL as the
bot's messaging endpoint.

## How it behaves

- Every request must carry a valid Bot Framework token, and replies only go to Microsoft's own
  service addresses.
- Personal chats, group chats and channel threads are separate conversations; in channels the bot
  answers when mentioned.
- Approvals and questions use Adaptive Cards.

Microsoft Graph change notifications (for example for mail or meetings) are a separate receiver;
see [API server and webhooks](api-and-webhooks.md#microsoft-graph-change-notifications).
