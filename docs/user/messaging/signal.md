# Signal

Indus Agent reaches Signal through **signal-cli**, which you install and run yourself; Indus Agent
only talks to it over HTTP on your machine. signal-cli is a separate program under its own licence
and is never bundled.

## What you need

1. Install signal-cli and link it to a phone number (a spare number, or link it as a second device
   of your own account).
2. Run its daemon with the HTTP interface:

    ```bash
    signal-cli --account +15550100 daemon --http 127.0.0.1:8080
    ```

## Setting it up

```bash
indus gateway setup signal
```

or set `SIGNAL_HTTP_URL` (for example `http://127.0.0.1:8080`) and `SIGNAL_ACCOUNT` (the number),
then `indus gateway start`.

## How it behaves

- Direct messages work after pairing (`indus pairing approve signal <code>`) or with your number in
  `INDUS_SIGNAL_ALLOWED_USERS`.
- Groups are ignored unless listed in the platform's allowed group chats.
- Phone numbers appear in logs only masked.
- The gateway reconnects to the daemon on its own and checks it when the event stream goes quiet.
