# SimpleX Chat

Indus Agent talks to SimpleX Chat through the SimpleX command-line client, which you install and
run yourself (it is a separate program under its own licence and is never bundled).

## Setting it up

1. Install the SimpleX Chat CLI and create the bot's profile in it.
2. Run it with its WebSocket API on your machine:

    ```bash
    simplex-chat -p 5225
    ```

3. Set `SIMPLEX_WS_URL` (for example `ws://127.0.0.1:5225`), or run `indus gateway setup simplex`,
   then `indus gateway start`.

## How it behaves

- Contacts become conversations after pairing or an allow-list, like other platforms.
- The gateway reconnects to the client on its own and checks it every 30 seconds.
