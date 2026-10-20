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
