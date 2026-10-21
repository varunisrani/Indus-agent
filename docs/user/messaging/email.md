# Email

Indus Agent can read a mailbox over IMAP and answer over SMTP. No extra libraries are needed.

## What you need

A mailbox for the agent (a dedicated address is best) with IMAP and SMTP access. With most
providers you create an app password for it.

## Setting it up

```bash
indus gateway setup email
```

or set `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_IMAP_HOST` and `EMAIL_SMTP_HOST`, then
`indus gateway start`. List the senders allowed to use it in `INDUS_EMAIL_ALLOWED_USERS`.

## How it behaves

- Each sender address is one conversation; replies are threaded under your mail.
- Mail already in the inbox when the gateway first starts is never answered.
- Auto-replies, bounces, list mail, mail from senders who are not allowed and mail whose sender
  domain the receiving server could not verify are dropped unread.
- New mail is noticed with IMAP IDLE where the server and Python support it, else by checking every
  minute.
- Attachments are passed to the agent.
