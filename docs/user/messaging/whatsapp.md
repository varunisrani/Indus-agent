# WhatsApp

There are two ways to connect WhatsApp, for two kinds of account.

| | WhatsApp Business (Cloud API) | WhatsApp (personal account) |
|---|---|---|
| Platform name | `whatsapp_cloud` | `whatsapp` |
| Account | a business phone number in Meta's Cloud API | your own WhatsApp account, linked like WhatsApp Web |
| Runs | inside the gateway; Meta calls a webhook | a bridge process on your machine |

## WhatsApp Business (Cloud API)

### What you need

From a Meta developer app with the WhatsApp product: a **permanent access token**, the **phone
number id**, the app's **app secret** (to check that webhook calls really come from Meta) and a
**verify token** of your choice for the webhook handshake.

### Setting it up

```bash
indus gateway setup whatsapp_cloud
```

or set `WHATSAPP_CLOUD_ACCESS_TOKEN`, `WHATSAPP_CLOUD_PHONE_NUMBER_ID`, `WHATSAPP_CLOUD_APP_SECRET`
and `WHATSAPP_CLOUD_VERIFY_TOKEN`. The gateway's webhook listener binds to loopback; put it behind
your HTTPS reverse proxy or tunnel, then enter that public URL and your verify token in Meta's
webhook settings and subscribe to `messages`. Without the app secret the gateway refuses to receive
anything, since unsigned calls could come from anyone.

### How it behaves

- Text is converted to WhatsApp's formatting and split at 4,096 characters.
- Approvals and questions use reply buttons (up to three choices) or a list (up to ten).
- Meta only allows free-form replies within 24 hours of the person's last message; a send outside
  that window is refused and not retried.

## WhatsApp (personal account)

The `whatsapp` platform runs a small bridge process (Node.js 24) that holds a WhatsApp Web session.
It needs a WhatsApp Web provider module that you install yourself and name in
`platforms.whatsapp.provider`; Indus Agent does not ship one. Then set
`platforms.whatsapp.enabled: true` and start the gateway. Linking the phone goes through the
provider module; until the account is linked the gateway reports the platform as not paired, and
`indus gateway status` says so.

Direct messages need pairing or an allow-list; in groups the bot answers only when mentioned,
replied to or addressed by one of its `mention_patterns`. A phone number and its WhatsApp alias
count as one person.
