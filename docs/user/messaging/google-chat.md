# Google Chat

Indus Agent works as a Google Chat app.

## What you need

A Google Cloud project with the Chat API enabled, a Chat app configured in it, and a **service
account** key (JSON) for the app.

## Setting it up

```bash
indus gateway setup google_chat
```

or set `GOOGLE_CHAT_SERVICE_ACCOUNT_JSON` to the key. Two ways to receive messages:

- `http` mode: Google calls the gateway's endpoint (port 8647, path `/google-chat`, loopback by
  default; publish it through your HTTPS reverse proxy). Google's token on each call is verified.
- `pubsub` mode: the gateway pulls from a Pub/Sub subscription, so nothing has to be public.

## How it behaves

- Direct messages and spaces work; in spaces the bot answers when mentioned.
- The cryptography library it needs comes with the standard install.
