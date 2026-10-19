# Wave 22 (P15-33..35 adapters) — parked builder patches, base 78be7b3

## w22-p15s.patch: P15-35 Slack (done)
- Builder checks: unit/gateway + contract/gateway 982 passed / 27 skipped; ruff, pyright, lint-imports, tooling.checks ok.
- Extra `slack = ["slack-bolt==1.30.0", "slack-sdk==3.44.1", "aiohttp==3.14.3"]` (MIT; aiohttp family already locked). Re-lock.
- Send-only seam: `AdapterConfig.send_only: bool = False`; send_command.build_adapter passes `send_only=True`. Reconcile with T/D (keep one shape).
- Wiring to do: daemon calls `adapter.register_fetchers(extras.intake)` per adapter (e.g. in `_connect` after
  ExtrasPart.prepare), undo on disconnect. Typing: pass `{"thread_id": thread_id or prospective_thread_id}` to TypingLoop.
- Core seam: top-level channel message keys its session on its own ts (prospective_thread_id) but the core takes the
  outbound thread only from `source.thread_id` → 2nd+ chunks of long replies and approval/clarify cards go top-level.
  Fix in `replies.final_reply_for` and `prompts._meta` for Slack without changing Discord's meaning.
- Decisions: one workspace per adapter; one session per DM channel; edit over cap refused; sender splits; mpim = group;
  settings ignored_channels, reply_in_thread (default on), typing_status_text. Fetcher sends the bot token only to https *.slack.com.

## w22-p15d.patch: P15-34 Discord (done)
- Builder checks: gateway/contract/fakes/send 1087 passed / 43 skipped without discord.py; 1118 passed with it (drift rows
  pass against the real lib); ruff, pyright, lint-imports, tooling.checks ok.
- Extra `discord = ["discord.py==2.7.1"]` (MIT) + audioop-lts 0.2.2 (PSF-2.0, py3.13+). Re-lock.
- Same send-only seam as Slack (`AdapterConfig.send_only`, send_command passes True): keep one copy.
- Wiring: `register_fetchers(intake) -> undo` uncalled (suggest ExtrasPart on daemon.on_platform_up/down);
  `DiscordAdapter.extra_commands` empty until the daemon passes plugin/skill command names.
- Decisions: thread-open failure falls back to parent channel; backfill also on thread-opening mention, prepended to event
  text; command typed with a mention gets no thread; discord.py loaded via importlib; no CommandTree.
- Follow-ups: ACC-K13-1 per-platform dedupe review line; forum posts, forwarded content, username→id allowlist resolution,
  missed-message recovery scan, reactions, voice (P3).

## w22-p15t.patch: P15-33 Telegram (done)
- Builder checks: gateway/contract/send/fakes/integration-gateway 1145 passed / 15 skipped; ruff, pyright, lint-imports, tooling.checks ok.
- No new packages: own async Bot API client over httpx (bot_api.py; python-telegram-bot is LGPL-3.0). Token in URL path
  only on a copied request via a wrapping transport; placeholder `/bot****/` everywhere else; errors scrubbed, `from None`.
- DIFFERENT seams from S/D: send-only via `BaseAdapter.connect(..., send_only=False)` (sets `self.send_only`) instead of
  `AdapterConfig.send_only`; fetchers via `fetch_media` + new `extras.attach_fetcher()` in ExtrasPart on platform up/down
  instead of `register_fetchers(intake)`. Integrator keeps ONE shape for each.
- Webhook listener default bind 0.0.0.0:8443 — lead ruling: default 127.0.0.1, public bind only by explicit config.
- Decisions: no telegram extra; webhook secret/url from secret store then env; cold boot drops backlog unless
  drop_pending_on_cold_boot: false; reactions and topic reminder opt-in; inbound edits ignored; command menu capped 3,500
  chars; forum chats get chat-scoped menus; refused press = empty callback answer; retries via shared policy.
- Follow-ups: runner should call processing_started/finished; revoked token (polling_stopped == "auth") should be a fatal
  platform state; TELEGRAM_DOCUMENTED_METHODS hand-kept.
