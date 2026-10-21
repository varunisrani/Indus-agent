# Adding a messaging platform

A platform is an adapter (`indus.gateway.adapters.base.Adapter`) plus an entry in the platform
registry (`indus.gateway.registry`).

## The adapter

Subclass `Adapter` and implement its small hooks: `_open` and `_close` (connect and disconnect),
`_send` (and `_edit`, typing or reactions when the platform has them). The base class gives every
adapter the same guarantees: connecting twice is harmless, exceptions become classified send
results with credentials masked, one send at a time per chat, and optional operations the platform
lacks are refused cleanly. Declare what the platform can do in its `Capabilities` (edits, buttons,
threads, message length, whether direct messages are really private); the rest of the gateway asks
the capabilities, never the platform's name.

Inbound messages become `InboundEvent`s handed to `emit`; the gateway's admission pipeline then
applies authorization, pairing, mention rules and the bot-loop guard. Keep the adapter free of
policy: it reports what happened (who sent it, in which chat and thread, whether the bot was
mentioned) and the core decides.

A webhook-based platform uses the shared listener kit, which binds to loopback unless opened on
purpose, limits body sizes and verifies signatures before parsing.

## The registry entry

A `PlatformEntry` names the factory (imported lazily, so the SDK is loaded only when the platform
is enabled), the credential variables, the allow-list variable names, the message limit, an install
hint and an optional target parser for `indus send` and scheduled-job delivery. Plugins add
platforms through their context in the same shape.

## Tests

Every platform ships a wire fake (a stand-in for the vendor's API at the HTTP or socket level) and
runs the shared adapter contract suite over it; CI fails when a registered platform lacks either.
Then add a page under `docs/user/messaging/` and regenerate the reference pages.
