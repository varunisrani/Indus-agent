# iMessage

iMessage needs a Mac signed in to your Apple account. Indus Agent supports two bridges.

## BlueBubbles

[BlueBubbles Server](https://bluebubbles.app) runs on the Mac and exposes iMessage over a REST
API. Install it (version 1.9 or later), set a server password, and note the server's URL.

```bash
indus gateway setup bluebubbles
```

or set `BLUEBUBBLES_SERVER_URL` and `BLUEBUBBLES_PASSWORD`. The gateway registers a webhook with
the server (port 8645, path `/bluebubbles-webhook`) to receive messages; if the gateway does not
run on the same Mac, publish that endpoint so the server can reach it and set
`platforms.bluebubbles.webhook_public_url`. The password never appears in logs.

## Photon

The `photon` platform uses a Photon Spectrum account (`PHOTON_PROJECT_ID`,
`PHOTON_PROJECT_SECRET`) through a small sidecar process (Node.js 24). It needs a Spectrum provider
module you install yourself and name in `platforms.photon.provider`, and it is off until you set
`platforms.photon.enabled: true`.
