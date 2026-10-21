# Web dashboard

```bash
indus dashboard            # serves http://127.0.0.1:9119 and opens your browser
```

The dashboard is a browser view of every profile on this machine: conversations (with a chat tab
that runs the terminal UI in the page), settings and keys, logs and usage, scheduled jobs, skills,
plugins and MCP servers, messaging channels, webhooks and pairing requests, profiles and models.

One dashboard backend serves the whole machine. Running `indus dashboard` again opens the browser
on the one already running; `--isolated` starts a separate one for this profile only. `--no-open`
skips the browser, `--port 0` picks any free port.

## Signing in

On the loopback address (`127.0.0.1`, the default) the dashboard needs no login: only programs on
this machine can reach it, and the browser gets a session from the address the command opens.

Any other address, or a `dashboard.public_url` behind a proxy, turns sign-in on, and the dashboard
refuses to start until a login provider is configured. There is no switch that skips this. Two
providers exist:

**Username and password.** Run `indus dashboard --host 0.0.0.0` on a terminal: it offers to set a
username and password and stores only a hash of the password (`dashboard.basic_auth`). Or set
`INDUS_DASHBOARD_USERNAME` and `INDUS_DASHBOARD_PASSWORD` in the environment; they win over the
stored values, which makes rotating the password easy.

**Single sign-on (OIDC).** Point the dashboard at your identity provider with a public client (PKCE,
no client secret):

```bash
indus config set dashboard.oidc.issuer https://login.example.com
indus config set dashboard.oidc.client_id indus-dashboard
```

## Behind a reverse proxy

Set `dashboard.public_url` to the address people use (it must match what the browser sees) and list
the proxy's address in `dashboard.trusted_proxies` so its forwarded headers are believed. Loopback
proxies are always trusted; `*` and `/0` networks are refused.

## Headless backend

`indus serve` runs the same backend without the web pages: JSON and WebSocket only, for other
programs such as the desktop app. `indus serve ensure --json` finds or starts one and prints its
address; tokens are never printed.

## Settings

| Key | What it does |
|---|---|
| `dashboard.theme` | `system`, `light` or `dark` |
| `dashboard.show_token_analytics` | show the token and cost page |
| `dashboard.public_url`, `dashboard.trusted_proxies` | proxy setup (above) |
| `dashboard.basic_auth.*`, `dashboard.oidc.*` | login providers (above) |
