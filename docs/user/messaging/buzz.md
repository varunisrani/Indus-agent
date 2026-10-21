# Buzz

Buzz is a Nostr-based community relay for people and agents. The bot is a Nostr identity that is a
member of a Buzz community.

## What you need

The community relay's URL and the bot's Nostr **private key** (`nsec` or hex). Add the bot's public
key to the community as a member.

## Setting it up

```bash
indus gateway setup buzz
```

or set `BUZZ_RELAY_URL` and `BUZZ_PRIVATE_KEY` (kept in the keychain), then `indus gateway start`.

## How it behaves

- The gateway signs its own events and answers the relay's authentication challenges; if the relay
  rejects the key, the platform stops and says that the key is not a member.
- It checks the watched channels every few seconds for new messages.
