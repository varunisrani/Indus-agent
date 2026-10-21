# Raft

Raft keeps the conversation itself and only tells an outside agent when something may be waiting.
Indus Agent provides that agent's door: a small wake endpoint on your machine.

## Setting it up

1. Install the Raft command-line tool and sign in; set `RAFT_PROFILE` to the Raft profile to use.
2. Start the gateway (`indus gateway start`). It listens on `127.0.0.1` and writes the wake address
   and its token to `<profile home>/platforms/raft/bridge.json` (readable only by you).
3. Run `raft agent bridge` with the address from that file.

## How it behaves

- Only requests from this machine with the bridge token are accepted.
- A wake makes the agent fetch and answer what is waiting through the Raft tool.
