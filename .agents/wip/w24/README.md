# Wave 24 (P16 E4, E5) — parked builder patches, base 0a106d2

## w24-e4.patch: P16-04 WhatsApp Business Cloud + P16-10 SMS (Twilio) — done
- Builder checks: own + contract + http + schema 284 passed / 38 skipped; unit/gateway + unit/cli 2266 passed; ruff,
  pyright, lint-imports, tooling.checks ok. No new packages (own httpx calls). Both on the P16-01 kit.
- Shared: contract `AdapterCase.lacks` hunk applied verbatim from w23-e3.patch (merging E3 should be a no-op there);
  conftest case imports; config schema rows platforms.whatsapp_cloud / platforms.sms (+ test); traps R06.4.
- Wiring (core): registry rows
  `replace(_chat_entry("whatsapp_cloud", "WhatsApp Business (Cloud API)", sdk=("httpx",), extra_name="whatsapp",
  required_env=("WHATSAPP_CLOUD_ACCESS_TOKEN", "WHATSAPP_CLOUD_PHONE_NUMBER_ID"), max_len=4096, hint="You are talking
  through WhatsApp. Markdown is converted to WhatsApp formatting; avoid tables. A reply more than 24 hours after the
  person's last message cannot be delivered."), install_hint="pip install indus-agent (WhatsApp Cloud needs no extra)")`
  and `replace(_chat_entry("sms", "SMS (Twilio)", sdk=("httpx",), extra_name="sms", required_env=("TWILIO_ACCOUNT_SID",
  "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"), max_len=1600, hint="You are talking through SMS. Use plain text only and
  keep replies short (about 1,600 characters per message)."), install_hint="pip install indus-agent (SMS needs no extra)")`;
  BUILTIN_CREDENTIALS whatsapp_cloud: (ACCESS_TOKEN, PHONE_NUMBER_ID), sms: (ACCOUNT_SID, AUTH_TOKEN, PHONE_NUMBER).
  Extra secrets via read_secret: WHATSAPP_CLOUD_APP_SECRET, _VERIFY_TOKEN, _WABA_ID.
- Decisions: WhatsApp markup converted adapter-side (proposed P15-15 "whatsapp" Markup follow-up); missing WhatsApp app
  secret = fatal at connect; SMS without platforms.sms.webhook_public_url = fatal (no unsigned mode, D-16.25); SMS to
  another number dropped; WhatsApp id = wa_id digits (alt +digits), SMS id = E.164 (alt digits); dm_is_private.
- Follow-ups: move mask_number helpers into _identity.py; ACC-K9-2 / ACC-K10-4 rows missing; voice transcoding, media by
  link, outbound MMS, clarify "Other", health counters.

## w24-e5.patch: P16-23 LINE + P16-26 ntfy + P16-27 IRC — done
- Builder checks: own + contract 195 passed / 38 skipped (3x stable); adapters + contract 441 passed; ruff, pyright,
  lint-imports, tooling.checks ok. No new packages; no schema rows (free platforms.<name> keys).
- Shared: contract `AdapterCase.lacks` (same hunks as E3, plus tokens `media`, `caption`), linear `_long_text`; three case
  imports; traps R06.3, R06.11, R06.16, R06.24. LINE signature vectors in tests/fixtures/gateway/crypto_vectors/line_signature.json
  (computed with openssl from the vendor spec).
- Wiring (core): registry rows
  `replace(_chat_entry("line", "LINE", sdk=("httpx",), extra_name="line", required_env=("LINE_CHANNEL_ACCESS_TOKEN",
  "LINE_CHANNEL_SECRET"), max_len=20000, hint="You are talking through LINE. Replies are plain text (Markdown is removed);
  keep them short."), install_hint="pip install indus-agent (LINE needs no extra)")`,
  `replace(_chat_entry("ntfy", "ntfy", sdk=("httpx",), extra_name="ntfy", required_env=("NTFY_TOPIC",), max_len=4096,
  hint="You are talking through ntfy push notifications: plain text, at most 4096 bytes per message; anyone who knows the
  topic can read it."), install_hint="pip install indus-agent (ntfy needs no extra)", dm_is_private=False)`,
  `replace(_chat_entry("irc", "IRC", sdk=("ssl",), extra_name="irc", required_env=("IRC_SERVER", "IRC_NICKNAME"),
  max_len=450, hint="You are chatting on IRC: plain text only, short lines; people address you by your nick."),
  install_hint="pip install indus-agent (IRC needs no extra)")`.
  BUILTIN_CREDENTIALS line: (ACCESS_TOKEN, CHANNEL_SECRET), ntfy: (NTFY_TOPIC,), irc: (IRC_NICKNAME, IRC_SERVER) — the
  latter masks nick/server/topic in logs (token-lock key comes from these): integrator decides (separate lock-key seam?).
- Decisions: LINE max_len 20,000 UTF-16 (bubbles 4,500), all bubbles sent, held answer pushed after 600 s, served media cap
  50 MiB, files refused, media refused in send-only, https API bases; ntfy multi-topic, >4,096 bytes refused, token only to
  https or local; IRC FAIL codes best-effort, unaddressed lines to core mention policy, user_id_alt = lower-cased nick,
  send-only connects as <nick>-send, IRC_SASL_PASSWORD new env name; plaintext auth only with allow_plaintext_auth.
- Follow-ups: lacks into Capabilities; kit streaming replies; LINE X-Line-Retry-Key; ntfy inbound attachments; IRCv3
  account-tag; interrupt seam for held LINE answers; doctor lists port 8646.
