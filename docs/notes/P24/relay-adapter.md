# Task: Relay adapter and ops (P24-09) Phase: 24 Owner: wave-37 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/relay/adapter.py` (op
dispatch only: capability surface, `_outbound` / `_gated_op`, connect, inbound bridge and replay
dedupe, scope capture and `_with_scope`, `fronts_platform`, send / edit / delete / typing /
follow_up / send_media / prompt / react / thread_create / thread_rename, prompt ids), 
`gateway/relay/egress.py` (decline classification, first 120 lines), `gateway/relay/media.py`
(whole: upload / download by reference, 25 MB cap). Research 06 §3.17 (transport resolution),
§3.25 (relay ops), §3.5 (upstream authorization).
Evidence: none.

What it does (own words): every platform the connector advertises becomes one gateway adapter
(Telegram-through-the-relay is still platform `telegram` to the rest of the gateway). Its
capabilities are projected from that platform's descriptor (length cap and unit, markup, edits,
buttons when `prompt` is advertised). Outbound text, edits and typing become `op` requests on the
shared transport; `send_media` (a local file uploaded to the connector first and sent by
reference, at most 25 MB), `prompt` (buttons for approvals and clarify choices), `react`,
`thread_create` and `thread_rename` are used only when the descriptor advertises them, otherwise
they come back as a capability refusal (`unsupported`), which the outbound layer treats as "use the
fallback", never as an error shown to the user. Inbound events from the connector are normalised
into `InboundEvent`s; they are authorized upstream (the connector admits senders), so the adapter
declares `auth_upstream`. The profile the connector routed a chat to is remembered and stamped on
every outbound op's metadata, so a follow-up (a button press after a command) lands in the same
profile. The delivery router follows one rule: an enabled native adapter wins; the relay serves a
platform only when it advertises fronting it; a native adapter that is explicitly disabled never
hides an enabled relay.

Inputs → outputs: connector events → `InboundEvent`s through `emit`; gateway sends / edits /
media / approvals / clarify / reactions / threads → `op` frames → `SendResult`s; router
`(native state, relay fronts?)` → transport choice.

Files: `src/indus/gateway/relay/adapter.py`, `tests/fakes/platform/relay_wire.py`,
`tests/unit/gateway/relay/test_adapter.py`; shared: `src/indus/gateway/delivery/router.py`
(relay senders and the resolution rule).

Edge cases / failure modes:
- text over the descriptor's cap (in its unit) is refused locally as too long, nothing sent (the
  gateway's chunker splits before sending; `splits_natively` is false).
- connector result codes map onto our send classifier: `unavailable` (transient, retried),
  `timeout` (maybe delivered, never retried), `rate_limited` (with the server's wait),
  `chat_not_found` (chat gone), `thread_not_found` / `message_not_found` (sub-chat), `forbidden`,
  `bad_format`, `too_long`, `unsupported` (capability refusal), `declined` (the connector refused
  the destination: a forbidden reading, never laundered into a fallback op).
- an op the descriptor does not advertise never reaches the wire.
- a media file over 25 MB, unreadable or empty → failure before any upload.
- approval and clarify buttons carry our callback data (≤64 bytes) verbatim; a press comes back as
  an event with `button` set, which the core resolves like any platform's button.
- the transport is shared: the first adapter that connects starts it, the last one to disconnect
  stops it; a platform the connector does not front fails to connect (not retryable).
- profile / scope caches are bounded (oldest dropped).

Our design differences / decisions:
- One adapter class, specialised per descriptor (`create_relay_adapter`), so `platform` and
  `capabilities` stay class-level facts like every other adapter.
- Prompt ids: our callback data already names the request id; no per-process nonce or pending
  prompt registry in the adapter (the core's approval and clarify registries own expiry).
- Drafts, native task cards, Slack status-text and thread-anchor knobs, Discord interaction
  passthrough, auto-thread rename lane, egress-target attestation for `send_message` are not
  built (recorded "later").

Tests we will write:
- the shared contract checks over the fake connector (lifecycle, connect failure, send ids,
  split refusal, edit, typing, approvals with buttons, inbound shapes, error classification, no
  real network).
- ops not advertised → refusal and nothing on the wire (send_media, prompt → text fallback,
  react, thread_create, thread_rename, edit).
- advertised ops round-trip (media by reference, react, thread create / rename, follow_up).
- profile round-trip: inbound profile stamped on later outbound ops.
- router resolution table: native enabled / disabled / absent × relay fronts yes / no, and the
  `DeliveryRouter` picking the relay sender.

## Decisions for the lead
- Daemon wiring (not in my files): build `RelayLink` from `gateway.relay` + `INDUS_RELAY_SECRET`,
  start it, `create_relay_adapter` per descriptor, connect each, and `DeliveryRouter.attach(name,
  sender, via_relay=True)`; native platforms keep `attach(name, sender)`; pass
  `native_state=` (config: True enabled / False explicitly disabled / None unset) to the router.
- The relay adapters are not registry entries (their names are the fronted platforms'); the
  adapter matrix does not see them, so the contract run lives in `test_adapter.py`.

Compare checklist (filled after the rewrite; reopened `gateway/relay/adapter.py`,
`gateway/relay/egress.py`, `gateway/relay/media.py`):
- [x] every op the reference dispatches is handled, refused by capability, or recorded: send, edit, typing, follow_up, send_media, prompt, react, thread_create, thread_rename, chat_info, delete (tests: `tests/unit/gateway/relay/test_adapter.py::test_react_threads_follow_up_and_chat_info_round_trip`, `::test_media_goes_by_reference_after_a_signed_upload`, `::test_prompt_buttons_carry_our_callback_data`, `::test_delete_is_used_only_when_advertised`)
- [x] op not advertised → refusal, nothing on the wire (test: `::test_ops_not_advertised_are_capability_refusals_and_never_reach_the_wire`, `::test_an_edit_op_withdrawn_by_a_new_descriptor_is_refused`)
- [x] a connector decline is never laundered into a fallback op (test: `::test_result_codes_map_onto_the_send_classifier` — `declined` reads as forbidden, not unsupported)
- [x] media ≤25 MB, signed upload, by reference (test: `::test_media_over_25_mb_or_empty_is_refused_before_upload`, `::test_a_bad_media_signature_is_refused_by_the_connector`)
- [x] profile / scope / sender round-trip in outbound metadata (test: `::test_the_serving_profile_round_trips_in_outbound_metadata`)
- [x] relay events authorised upstream (test: `::test_inbound_events_are_normalised_and_authorised_upstream`)
- [x] revocation surfaces as a fatal on each adapter (test: `::test_a_revoked_relay_reports_fatal_to_its_adapters`)
- [x] contract suite (test: `::test_relay_adapter_passes_the_contract_suite`, `::test_relay_adapter_uses_no_real_network`)
- [x] router resolution table and the `DeliveryRouter` (test: `::test_router_resolution_table`, `::test_the_delivery_router_follows_the_rule`)
- [x] gap found: the reference gates `delete` on the descriptor — added (`delete` optional op, `supports_delete` from the descriptor), pinned by `::test_delete_is_used_only_when_advertised`
- Deliberate differences: one adapter per fronted platform (the reference: one adapter presenting as "relay" and routing per chat); inbound replay dedupe by buffer id in the transport rather than by (platform, chat, message id) in the adapter (live events are never replayed in our contract); prompt presses come back as our ordinary button callback data instead of a separate `prompt_response` field with a per-process nonce.
- Not taken: draft streams and native task cards, Slack status-text / thread-anchor knobs (`reply_in_thread`), auto-thread rename lane, the 👀→✅ processing reactions (the `react` op exists; hooking it to turn start/end is daemon wiring), egress-target attestation for the `send_message` tool, per-chat descriptor lookup for chats of several platforms on one adapter (not needed: one adapter per platform) — recorded "later".

## Wiring (wave 42, builder WA, 2026-10-21)

Read: no reference file (wiring). `gateway/relay/part.py` (`RelayPart`, BACKGROUND, only with
`gateway.relay.url`): `build_front` → the part holds the transport (`acquire`; a retryable first
failure is dialled again with the transport's backoff, a configuration problem is reported once;
status row `relay`) → one `create_relay_adapter` per descriptor, connected through
`GatewayDaemon.attach_relayed` (intake, sender, status, listeners like a native adapter; the
delivery router attaches it `via_relay` and reads `native_state` from the daemon) → a platform
whose native adapter is enabled is left to it. `RelayFront.on_interrupt` stops the running turn of
that session key (or every running turn of that platform and chat, in any served profile). At an
idle exit the part asks the connector to go dormant before the adapters disconnect; relayed
platforms do not keep an auto-started daemon up. Events the connector replays right after the
handshake wait for their adapters (`RelayFront.hold` / `release`: an event handed over is
acknowledged and would never be replayed again). Tests: `tests/integration/gateway/test_relay_part.py`.

Decisions for the lead: a relayed platform needs its registry entry (its access policy; an
unknown platform is denied even though relay events are `auth_upstream`); `relay_settings` loads
the profile config when the scope carries none.

## Status

Done (2026-10-21). Adapter, fake connector and router rule written and tested
(`tests/unit/gateway/relay/test_adapter.py`, 50 tests incl. the 10 shared contract checks); shared:
`gateway/delivery/router.py`. Daemon wiring done in wave 42 (above; `RelayFront.hold` / `release`
added).
