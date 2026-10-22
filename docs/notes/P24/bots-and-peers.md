# Task: Bot-to-bot messaging and peer gateways (P24-07) Phase: 24 Owner: wave-37 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `website/docs/user-guide/bot-mode.md`
("Bot-to-bot messaging", "What actually makes a chat a Bot Chat", "Failed turns retry safely",
"typed reasons", "Bot-initiated DMs across machines"), `hermes_cli/subcommands/peer.py` (verbs and
key handling), `gateway/hosted_room_peer.py` (skim: purpose, URL/transport rules, grants).
Research 06 §3.20 (bot-to-bot); research 08 R08.11.
Evidence: none.

What it does (own words):
- **Canonical chat.** Each profile ("bot") has one forever-chat. Its identity is only (profile, the
  fixed title `Bot chat`): titles are unique in a profile's store (unique index), so at most one row
  carries it. Lookup reads hidden rows too and follows the compression lineage to its live tip (the
  title moves to the newest segment when a chat is compressed). When nothing is found, the lookup
  runs again inside the write transaction (adopt before mint) and only then creates the chat,
  hidden from the session list. No session-id pointer is ever stored and recency is never consulted
  (R08.11: pointers dangled, recency heuristics stole the chat and forked it in loops).
- **`message_agent` tool** (only in the `indus-bots` bundle; its tool set is gated to the `bots`
  platform so "all toolsets" never brings it in): a bot sends a message to another local profile's
  canonical chat, or to a peer gateway's profile (`peer/profile`, or `peer` for the peer's default
  profile). The message is prefixed with the sender's attribution. Failures come back typed:
  `provider_auth`, `provider_quota`, `provider_rate_limit`, `provider_server`, `context_overflow`,
  `missing_config`, `model_unavailable`, `runtime_offline`, `queued_expired`, `delivery_timeout`,
  `target_busy`, `unknown`. A retryable failure (runtime offline, delivery timeout, rate limit,
  provider server error, context overflow) is retried once, never more; the others are returned at
  once (auth, quota and configuration cannot be fixed by trying again). A relayed cross-machine turn
  is capped at 10 minutes.
- **Peers.** `indus peer add NAME --url URL [--lan] [--key-stdin]`, `list`, `remove`, `status`,
  `dm`, `run`, `stop`. The registry (name, URL, LAN flag, note; never a key) is a 0600 JSON file in
  the profile home; the key lives in the profile's `SecretStore` under `INDUS_PEER_<NAME>_KEY`, which
  is also the env name read for headless use. A key is read from stdin or a hidden prompt, never
  from argv. Calls go to the peer's API server (P15-30) with `Authorization: Bearer <key>`; no
  redirects are followed (the key must not travel to another origin). Peer URLs pass the SSRF policy
  (`safe_client`, checked at connect time) unless the peer was added with `--lan`; a URL holding a
  credential is refused.
- **Receiving side.** The peer's API server serves `POST /v1/bots/chat` (resolve or create its
  canonical chat and answer its id) and `POST /v1/bots/dm` (one turn in the canonical chat, capped
  at 10 minutes, typed failure on error). `run`/`status`/`stop` use the existing `/v1/runs` routes
  with the canonical chat's session id.
- **`self-chat` cron target** (H11): `deliver: self-chat` injects the job's output into the job's own
  profile's canonical chat as an incoming (user-role) message, attributed to the job; no turn runs.

Inputs → outputs: `find_canonical(store)` / `ensure_canonical(store)` → session id;
`message_agent(target, message)` → JSON `{status: delivered, reply, target}` or `{status: failed,
reason, error, retried}`; `indus peer …` → stdout/JSON, exit 0 ok, 1 delivery or peer error, 2 usage;
cron `deliver: self-chat` → a user-role row in the canonical chat.

Files: `src/indus/state/bot_chat.py` (new, below every surface: layer rule D-16.246),
`src/indus/service/peers.py` (new: peer book and HTTP client, shared by the CLI and the gateway),
`src/indus/gateway/rooms/peers.py` (receiving routes, local delivery transport, self-chat
deliverer), `src/indus/tools/builtin/message_agent.py`, `src/indus/cli/commands/peer.py`,
`tests/unit/state/test_bot_chat.py`, `tests/unit/gateway/rooms/test_peers.py`,
`tests/unit/tools/builtin/test_message_agent.py`, `tests/unit/cli/commands/test_peer.py`. Shared:
`src/indus/tools/toolsets.py` (`bots`, `indus-bots`), `src/indus/foundation/errors.py`
(`peers.failed`), `src/indus/cli/app.py` (`peer` row), `src/indus/cron/targets.py` (`self-chat` known),
`src/indus/gateway/delivery/router.py` (self-chat deliverer attached with the router),
`tests/contract/cli/test_help_catalogue.py`, `tests/contract/cli/non_tty_table.yaml`.

Edge cases / failure modes:
- two writers creating the canonical chat at once: the unique title index makes the second create
  fail; it re-runs the lookup and adopts the first one's row.
- the canonical chat was compressed: lookup lands on the tip, the route stays stable.
- an ambiguous lineage (two live continuations) fails closed (`AmbiguousChain`) rather than guess.
- the title held by a non-canonical session of the user's: the user's row *is* the canonical chat
  (identity is the title); nothing is renamed.
- `message_agent` to itself or to an unknown local profile → refused, no delivery; an empty message
  refused; a peer target whose peer is not registered → `missing_config`.
- peer without a key → refused before any request (`missing_config` for the tool, exit 1 for the
  CLI, with the command that sets it); peer URL with userinfo or a credential → refused at add.
- peer answers 401/403 → `provider_auth`; 429 → `provider_rate_limit`; 5xx → `provider_server`;
  connect error → `runtime_offline`; read timeout after the request went out → `delivery_timeout`
  (the CLI says do not resend: the turn is running there).
- the key never appears in argv, logs, URLs or exception text (errors are built from status codes
  and the peer's error code, not the request).

Our design differences / decisions:
- Our names: title `Bot chat`, tool `message_agent`, bundle `indus-bots`, gated set `bots`, routes
  `/v1/bots/chat` and `/v1/bots/dm`, env `INDUS_PEER_<NAME>_KEY`, registry `<home>/peers.json`,
  reason codes without the reference's `_or_access` / `_limit` suffixes (`provider_auth`,
  `provider_quota`, `provider_server`).
- Peer names/URLs live in a state file, not in `config.yaml`, so `indus peer add` never rewrites
  the user's config.
- The message travels as a JSON field, never through a shell. Delivery here is synchronous (the
  tool waits for the reply, bounded by the 10-minute cap) rather than fire-and-forget with a
  background notification; recorded as a difference.
- `--lan` marks a peer whose URL is private/loopback on purpose; without it the SSRF policy applies.
- Left out: the desktop courier for connections, the teammate roster in the system prompt, the
  `@mention` composer, live-delivery receipts under a runtime folder, cross-gateway room grants
  (`hosted_room_peer.py` RoomLink grants) — later rows (P24-31 desktop bots) or "later".

Tests we will write:
- canonical chat: created hidden once; found again including hidden; survives compression (same
  route, tip resolved); a racing creator adopts; never consults recency (a newer untitled session is
  ignored); ambiguous lineage fails closed.
- message_agent: each failure text/status maps to its reason; retryable failure retried once
  (second success delivered, two failures → failed with `retried: true`); non-retryable not
  retried; self and unknown targets refused; attribution prefix present; peer target parsing.
- peers: peer without a key refused before any request; SSRF refuses a private URL unless `--lan`;
  no redirect is followed; status → reason mapping; the key never in the error text; registry file
  holds no key and is 0600.
- self-chat cron target lands as an incoming message in the canonical chat (and creates the chat
  when missing); the receiving `/v1/bots/chat` route returns the same id twice.
- CLI: `peer add/list/remove`, `dm` without a key exits 1 with guidance, usage errors exit 2.

## Decisions for the lead
- **API server mounting (shared file, M1 edits it):** `gateway/rooms/peers.py:bots_router(ctx)`
  must be included in `build_api_app` beside `runs.router(ctx)`; until then `/v1/bots/*` answers
  404 on a real gateway. One line.
- **Who provides the transport:** `message_agent` reads its transport from the handler extra
  `bot_transport` when the turn offers one, else from `install_transport(profile key, transport)`;
  the gateway daemon should call `message_agent.register_tools(registry)` and install
  `LocalBotTransport` (gateway/rooms/peers.py) at start. The tool is deliberately not in
  `BUILTIN_MODULES`: a gated tool there fails the "every builtin reachable from all" contract,
  and like the Feishu / Yuanbao tools it belongs to the host that can deliver. Without a
  transport the tool answers `runtime_offline` ("bot messaging is not available here").
- **`self-chat` outside the gateway:** the deliverer is registered while a `DeliveryRouter`
  exists (gateway-hosted cron). `indus cron tick` run from the CLI has no router, so a
  `self-chat` job there reports a missing deliverer; follow-up: register
  `gateway.rooms.peers.install_self_chat` (or a state-level twin) in the CLI cron host.
- **Turn toolsets on the receiving route:** `/v1/bots/dm` runs through the API server's
  `SessionDesk`, whose sessions get `indus-api-server`; canonical chats should run with
  `indus-bots` / platform `bots` (desk option or a bots desk) — wiring for the lead.
- **Sessions that get the tool:** canonical chat turns must select `indus-bots` with platform
  `bots`; nothing else ever sees `message_agent`.
- New files outside the plan's list (layer rule): `state/bot_chat.py`, `service/peers.py`.
- Typed reason names (above) and the synchronous delivery model.

Compare checklist (filled after the rewrite; reopened `hermes_cli/subcommands/peer.py`,
`gateway/hosted_room_peer.py` (module purpose, URL rules) and the bot-mode doc's bot-to-bot
sections):
- [x] peer verbs add/list/remove/dm/run/status/stop with `ls`, `rm`, `set` aliases (tests: `tests/unit/cli/commands/test_peer.py`, contract non-TTY rows)
- [x] key name per peer, env fallback for headless (test: `tests/unit/gateway/rooms/test_peers.py::test_key_comes_from_the_store_or_the_env_name`)
- [x] peer without a key refused before any request (test: `::test_peer_without_a_key_is_refused_before_any_request`)
- [x] the key never follows a redirect (test: `::test_a_redirect_is_not_followed`)
- [x] canonical lookup includes hidden rows, adopt before mint, survives compression (tests: `tests/unit/state/test_bot_chat.py`)
- [x] typed reasons and at most one retry, only when it can help (tests: `tests/unit/tools/builtin/test_message_agent.py`)
- [x] `peer/profile` target goes to the peer's `/p/<profile>` prefix (test: `test_peers.py::test_profile_target_uses_the_profile_prefix`)
- [x] a bare silence reply comes back empty, on both doors (tests: `test_peers.py::test_peer_dm_reaches_the_canonical_chat_of_the_peer`, `::test_local_exit_codes_map_to_outcomes`)
- [x] a timeout after the request went out says "do not resend" (test: `test_peers.py::test_a_read_timeout_says_do_not_resend`)
- Deliberate differences: delivery is synchronous with the 10-minute cap (no background
  completion notification); peers live in `<home>/peers.json`, not `config.yaml`; `peer remove`
  also deletes the stored key; `peer dm` reads stdin only for `-` (never implicitly: R04.19);
  redirects are refused instead of followed-with-the-key-stripped; the SSRF policy applies to peer
  URLs unless `--lan`; our reason names (`provider_auth`, `provider_quota`, `provider_server`);
  canonical chat resolution happens on the peer's side (`/v1/bots/chat`), not by listing its
  sessions.
- Not taken: idempotency keys for `peer run` and the durable-run capability probe (our runs API
  has neither), the desktop courier and roster prompt section, live-delivery receipts,
  cross-gateway room grants (RoomLink), the `ui_meta` install marker (our gate is the `bots`
  platform of the canonical chat's turns).

## Integration (P24 integrator, 2026-10-21)

Wired: the API app includes `bots_router(ctx)`; the `self-chat` deliverer moved to
`indus.service.self_chat` (re-exported here) so `indus cron tick` / `cron daemon` hold it during
their passes (the CLI may not import the gateway). Owed: `message_agent` registration with the bot
transport, the room loop, idle exit counting live room tasks (D-16.799).

## Wiring check (wave 42, builder WA, 2026-10-21)

Read: no reference file. `bots_router(ctx)` is served by the daemon's own API app
(`build_api_app`, behind its bearer gate): `tests/unit/gateway/rooms/test_peers.py::test_the_api_server_itself_serves_the_bot_routes`.
The room loop and idle exit counting room tasks are wired (room-driver note). `message_agent`
registration with the bot transport is slice WB's.

## Wiring (wave 42, WB — D-16.799)

Wired: `message_agent` is registered in the daemon's tool registry (`gateway_tool_registry`, with the
Feishu / Yuanbao / `a2a_call` tools) and the chat service installs `LocalBotTransport` for the profile
(`_install_bot_transport`, undone at daemon stop). Tests: `tests/unit/gateway/daemon/test_host_tools.py`.
Still owed (follow-up, not WB's list): canonical-chat turns run with `indus-bots` / platform `bots`
(the API server's `/v1/bots/dm` desk and the desktop Bots page still open them with their own
platform, so `message_agent` is reachable only once that selection is made). The room loop is wired by WA (above).

## Status

Done (2026-10-21); daemon side wired in wave 42.
