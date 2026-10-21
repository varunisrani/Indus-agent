# ntfy

[ntfy](https://ntfy.sh) is a simple publish-subscribe notification service. Indus Agent can listen
on a topic and answer on it, which makes a quick private channel to your phone.

## Setting it up

Set `NTFY_TOPIC` to a topic name that is hard to guess (anyone who knows a topic's name can read and
write it), and optionally the server, then start the gateway:

```bash
indus config set NTFY_TOPIC indus-7f3c9a2e41
indus gateway start
```

Subscribe to the same topic in the ntfy app and write to it.

## How it behaves

- ntfy has no verified senders, so the topic is the identity: allow the topic in
  `INDUS_NTFY_ALLOWED_USERS` (or let everyone on it in). Pairing codes are never posted on a topic,
  because anyone could read them.
- Replies are published back to the topic, at most 4,096 bytes each.
- The subscription reconnects on its own and catches up on messages published while it was away.
