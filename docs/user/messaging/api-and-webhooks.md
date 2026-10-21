# API server and webhooks

Besides chat apps, the gateway can take work from programs.

## API server

An OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/responses`), so tools that speak
either API can use Indus Agent (with its tools and memory) as if it were a model. A request can
continue a stored session by sending its id in the `X-Indus-Session-Id` header.

```bash
indus config set gateway.api_server.enabled true
indus gateway api-key create --label my-app      # prints the key once
indus gateway restart
```

It listens on `127.0.0.1:8642` by default. Every request needs a key, also from this machine.
`/v1/models` lists one model, `indus-agent` (`gateway.api_server.model_name`). Requests run with
nobody to approve risky commands, so those are refused unless `approvals.unattended_mode` is
`approve`. Browser origins must be listed in `gateway.api_server.cors_origins`.

## Webhooks

Webhook routes start an agent run when another service posts an event, for example a review of
every new pull request.

```bash
indus config set gateway.webhook.enabled true
indus config set GITHUB_WEBHOOK_SECRET <the secret you set on GitHub>
indus webhook subscribe github-prs --auth github --secret-name GITHUB_WEBHOOK_SECRET \
    --events pull_request --prompt "Review {pull_request.html_url}" --deliver telegram
indus webhook list
indus webhook test github-prs
```

The receiver listens on `127.0.0.1:8644`; publish it through your HTTPS reverse proxy. Each route
names a stored secret used to check the sender's signature, has a rate limit, and runs with a fixed,
read-only toolset (web search, reading pages, looking at images, asking a question). The result can
be delivered to a chat like a scheduled job's.

## Microsoft Graph change notifications

The `msgraph` receiver takes change notifications for Graph subscriptions you create (mail,
calendar, meetings). It answers Graph's validation handshake, checks the `clientState` secret you
set in `MSGRAPH_WEBHOOK_CLIENT_STATE` on every notification, and drops repeats. It listens on port
8646, path `/msgraph/webhook`, on loopback unless opened; a public bind also needs the allowed
source address ranges.
