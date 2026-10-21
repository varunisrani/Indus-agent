# SMS

Indus Agent sends and receives text messages through Twilio.

## What you need

A Twilio account with a phone number that can send SMS: the **Account SID**, the **Auth Token** and
the number in international format.

## Setting it up

```bash
indus gateway setup sms
```

or set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_PHONE_NUMBER`. To receive messages,
Twilio must reach the gateway's webhook (port 8080, path `/webhooks/twilio`, loopback by default):
publish it through your HTTPS reverse proxy or tunnel, set that URL as the number's messaging
webhook in the Twilio console, and set the same URL in `platforms.sms.webhook_public_url`. Twilio
signs each request over that URL, so the gateway refuses to receive without it.

## How it behaves

- Plain text only, split into pieces of at most 1,600 characters.
- Opt-out, opt-in and help keywords are left to Twilio, which keeps the opt-out list.
- Picture messages (MMS) are passed to the agent as attachments.
- Phone numbers are masked in logs.
