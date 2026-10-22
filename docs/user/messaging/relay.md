# Relay

A relay lets a gateway without a public address answer on chat platforms. A relay connector is a
service on the internet that holds the platform connections (bot tokens, webhooks); your gateway
dials **out** to it over one WebSocket, and messages travel both ways on that connection. Nothing
has to reach your machine from outside.

The connector speaks Indus's own relay protocol. If you run or write one, its side is described in
the [relay connector contract](../relay-contract.md).

## Setting it up

```bash
indus config set gateway.relay.url https://relay.example.com
indus config set gateway.relay.gateway_id my-gateway
indus config set INDUS_RELAY_SECRET <the shared secret the connector gave you>
indus gateway restart
```

The secret is stored like any other credential, never in `config.yaml`. With `gateway.relay.url`
empty (the default) there is no relay. A wrong URL, id or secret is reported once and not
retried; a connection that fails for other reasons is tried again (below).

On connect, the gateway proves who it is with the shared secret, and the connector says which
platforms it fronts for this gateway. Each becomes an ordinary platform to the rest of Indus:
Telegram through the relay is still `telegram` for access rules, delivery targets and scheduled
jobs. The connector decides who may write to the bot, but a relayed platform still needs its usual
access settings (see [who may talk to the bot](index.md#who-may-talk-to-the-bot)).

## What works through a relay

Text, edits and typing always work. Files (up to 25 MB), buttons (approvals and choices),
reactions and threads work when the connector says the platform supports them; otherwise Indus
falls back as it does on a platform without that feature. A stop sent from the chat stops the
running turn.

When a platform has its own adapter turned on in this profile, that adapter is used and the relay
is not.

## Dropped connections and idle gateways

A dropped connection is dialled again after 1 second, then 2, 4 … up to 60 seconds between tries.
When an automatically started gateway goes idle, it asks the connector to hold its messages and
closes the connection; the connector replays them, in order, on the next connect, and each is
acknowledged only once the gateway has taken it, so nothing is lost or handled twice.

| Setting | Default | Meaning |
|---|---|---|
| `gateway.relay.url` | empty | the connector's base URL; empty means no relay |
| `gateway.relay.gateway_id` | empty | this gateway's id at the connector |
| `gateway.relay.connect_timeout_seconds` | `30` | seconds to connect and finish the handshake |
| `gateway.relay.request_timeout_seconds` | `30` | seconds to wait for the answer to one operation |
