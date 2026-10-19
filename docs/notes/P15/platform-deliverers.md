# Task: Platform deliverers, router, channel directory, mirror, `indus send` (P15-20) Phase: 15 Owner: wave-2 builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/delivery.py` (all),
`gateway/channel_directory.py` (all), `gateway/mirror.py` (all), `tools/send_message_targets.py`,
`hermes_cli/send_cmd.py`. Research `06-gateway.md` (delivery, K40–K42), P14 landing notes (target
grammar, deliverer registry), trap R02.26. Closed before writing.
Evidence: none.

What it does (own words): one library sends text (and trusted files) to a chat named by a P14
`DeliveryTarget`, for cron output, `indus send` and anything else that is not a reply to an
inbound message. When a platform's adapter connects, the router registers a deliverer for it with
P14's `cron.delivery.register_deliverer` (and one for `all` while any platform is attached); a
fatal disconnect removes it. A deliverer resolves the target: a bare platform → its home channel
(none → `no_home_channel`), `platform:chat[:thread]` → that chat (`#name` looked up in the
channel directory), `origin` was already resolved by P14, `all` → every attached platform's home
channel at send time. A connected native adapter is the only transport for now (relay is P24).
Cron output longer than 4,000 characters, on a platform whose adapter does not split natively, is
cut with a pointer to the saved output file. Sends go through the platform's `OutboundSender` as
unsolicited deliveries (dead targets skipped, formatting, chunking, masking); media tags in the
text are never honoured — only files the caller hands over as trusted paths (cron's checked
attachments, `indus send --file` after the P15-18 path policy) are attached.

The channel directory lists the chats each connected platform can reach: the adapter's own
`list_channels()` when it has one, else the chats the routes index has seen. It is built at
startup, refreshed every 5 minutes, and saved as JSON in the profile home so `indus send` in
another process can resolve names. A mirror note goes to the conversation that lives in a chat
when something is delivered there from elsewhere, so its agent knows on its next turn (staged as
a must-deliver note when the session runs in this process, else kept in session metadata and
picked up by the gateway at that session's next turn).

`indus send TARGET [TEXT] [--file PATH] [--subject LINE] [--list] [--json] [-q]` parses TARGET
with the P14 parser and sends through the router using one-shot adapters built from the platform
registry. The body is the positional text or piped stdin; `MEDIA:` tags in it are sent as text,
never as attachments; `--file` is the only attachment and must pass the gateway media path policy.
The router is a library: no model-visible tool wraps it (R02.26).

Inputs → outputs:
- `DeliveryRouter(scope, *, homes, directory=None, mirror=None)`; `attach(platform, adapter,
  sender)` / `detach(platform)` / `close()`; `await send(target, text, *, files=(), label,
  output_path=None) -> DeliveryReceipt`; `deliverer(platform)` is what gets registered.
- `ChannelDirectory(scope, path=None)`: `build(adapters)`, `run(adapters, interval=300)`,
  `load()`, `resolve(platform, name)`, `entries(platform)`, `render()`.
- `Mirror(store, service=None)`: `record(platform, chat_id, thread_id, text, label)`,
  `stage_pending(session_id)`; `take_notes(store, session_id)`.
- `indus.gateway.delivery.send_command:send_command` (registered by string in `cli/app.py`).

Files: `src/indus/gateway/delivery/router.py`, `src/indus/gateway/delivery/directory.py`,
`src/indus/gateway/delivery/mirror.py`, `src/indus/gateway/delivery/send_command.py`,
`tests/unit/gateway/delivery/test_router.py`, `tests/unit/gateway/delivery/test_directory.py`,
`tests/unit/gateway/delivery/test_mirror.py`, `tests/unit/cli/commands/test_send.py`.
Shared: `src/indus/cli/app.py` (REGISTRY row), `tests/contract/cli/test_help_catalogue.py`,
`tests/contract/cli/non_tty_table.yaml`, `src/indus/gateway/adapters/base.py`
(`list_channels()` default), `src/indus/gateway/core/admission.py` (stages pending mirror notes
before a turn).

Edge cases / failure modes:
- `telegram` with no home channel → `no_home_channel` failure; `all` with nothing attached →
  failure naming that; `all` skips platforms without a home and reports them.
- `#name` not in the directory → `unknown_channel`; an exact id in the directory wins over names;
  an ambiguous prefix resolves to nothing.
- a deliverer for a platform whose adapter is gone → `platform_not_connected`.
- truncation only with a saved output file to point at; never for native splitters.
- a mirror into a chat with several conversations (group, per-user) and no way to tell → skipped
  with a log line (never guess).
- directory file corrupt or unwritable → empty directory in memory; delivery still works.
- `indus send` to a disabled platform, a missing library, an unknown platform, `origin`/`local`
  → usage-style errors (exit 2) before any network; delivery failure → exit 1.

Our design differences / decisions:
- `--file` attaches a file (the reference reads the body from it); the body comes from the
  argument or stdin. Attachments pass `MediaPolicy.check` (deny-list, symlinks, trusted folders).
- `indus send` builds its own adapters (no running gateway needed) and connects them for the send;
  adapters that start a long poll on connect need a send-only open (follow-up for P15-33..35).
- Mirror notes ride the next user message (P04 note channel), never a fake assistant message.
- Directory entries come from adapters or the routes index only (no alias overlay file; P2).
- Silence-narration filtering of cron output belongs to P15-21.

Tests we will write: attach registers in P14's registry and detach removes it; `all` registered
while any platform is attached and resolved at fire time; `telegram` with no home →
`no_home_channel`; `#general` resolved via the directory; truncation footer; media tags in cron
text not attached, trusted attachments sent; dead target skipped; mirror note staged/stored;
directory build from adapter and routes, persistence, refresh with the fake clock, resolve rules;
`indus send` happy path through a fake registry entry, `--file` refused by the policy, MEDIA tag
sent as text, unknown platform, stdin body, `--list`; R02.26 snapshot: no model tool wraps the
router.

## Decisions for the lead
- `indus send` lives in `indus.gateway.delivery.send_command` and is registered by string (layer
  rule D-16.246), after `pairing` in the help order.
- `Adapter.list_channels()` (default None) added to the adapter base for the directory.
- One-shot adapters in `indus send` call `connect()`; P15-33..35 should make that cheap or add a
  send-only mode before Telegram long polling would compete with a running daemon.

Compare checklist (filled after the rewrite):
(reopened `gateway/delivery.py`, `gateway/channel_directory.py`, `gateway/mirror.py`,
`tools/send_message_targets.py`, `hermes_cli/send_cmd.py`)
- [x] target kinds through P14's parser and our deliverers only; no grammar code in `gateway/`
  (`tests/unit/gateway/delivery/test_router.py::test_home_channel_target_with_thread`,
  `::test_explicit_chat_and_thread`, `::test_origin_resolves_to_the_jobs_chat`,
  `::test_all_is_resolved_at_fire_time`, `::test_no_home_channel_is_a_named_failure`)
- [x] registration on connect, removal on fatal disconnect, re-registration on reconnect
  (`::test_attached_platforms_register_with_p14_and_go_on_detach`, `::test_reconnect_registers_again_without_leaving_a_stale_deliverer`)
- [x] native transport only; not connected → named failure (`::test_not_connected_platform`)
- [x] truncation over 4,000 with a pointer, never for native splitters (`::test_long_cron_output_is_cut_with_a_pointer`, `::test_native_splitters_are_never_cut`)
- [x] dead targets skipped (`::test_dead_chat_is_skipped`)
- [x] directory: adapter listing or seen chats, saved, refreshed every 5 min, id → name → guild/name → unique prefix
  (`test_directory.py::test_build_from_adapter_and_routes_and_persist`, `::test_resolve_rules`, `::test_refresh_every_five_minutes`)
- [x] mirror: one conversation or none, note reaches the next turn (`test_directory.py::test_mirror_finds_one_conversation_or_none`,
  `test_turn_wiring.py::test_a_kept_mirror_note_rides_the_sessions_next_turn`, `test_router.py::test_delivered_cron_output_leaves_a_mirror_note`)
- [x] `indus send` (`tests/unit/cli/commands/test_send.py`), R02.26 (`test_router.py::test_no_model_tool_wraps_the_router`)
- Deliberate differences: `--file` attaches (through the media policy) instead of reading the
  body; media tags in delivered text are never followed; the mirror note rides the next user
  message instead of a transcript row; no alias overlay file; no `--mention` (WhatsApp, P16).
- Not taken here: the silence-narration filter (P15-21); Telegram named DM topics (P3); the relay
  transport (P24); Slack/Discord channel enumeration (their adapters implement `list_channels`,
  P15-34/35).
