# Task: Bots in the desktop app (P24-31) Phase: 24 Owner: wave-39 builder U2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/plugins/<bots plugin>/`
(file list; `canonical-chat.ts` for the lookup and the in-flight creation map). Research 08 §3.20,
R08.11. Our `docs/notes/P24/bots-and-peers.md`, `room-store.md`, `room-driver.md` (parked in
`.agents/wip/w37/w37-r1.patch`: `state/bot_chat.py`, `gateway/rooms/rpc.py`). Closed before writing.
Evidence: none.

What it does (own words): a Bots surface lists every profile as a bot: its avatar, its routines
(the profile's cron jobs), the sections the person sorted it into, and whether it is hidden from the
list. Clicking a bot opens its one forever-chat. That chat's identity is only (profile, the fixed
canonical title "Bot chat"): the daemon looks it up by title in the profile's store (hidden rows
included, compression lineage followed to the live tip), looks again inside the write when it finds
none (adopt before mint), and only then creates it hidden. Nothing stores a session-id pointer and
nothing picks "the most recent chat". The renderer keeps one in-flight open per bot, so a double
click opens one chat; the server's adopt-before-mint makes two windows safe too. The daemon reports
the canonical row (root id and live tip id) on the profiles list, so the row's preview and the click
open the same chat. Canonical chats never show in the sessions sidebar; side-chats of a bot do.

Avatars: a geometric face we generate from a seed (deterministic SVG: face shape, eyes, mouth and
colour from the seed's hash bytes), an uploaded image (data URL, ≤ 256 KiB, png/jpeg/webp), an AI
portrait (an image the person generates elsewhere and uploads — no generation call here), or the
profile's pet (P24-19, by reference).

Group rooms (P24-05/06) show text-only: the room log (`groups.log` paged by `since_seq`), a composer
that sends with `groups.send` under a client event id (a retry re-sends the same id, so a lost answer
never posts twice), Stop sends `groups.stop` with a fresh cancel id; pending actions (Retry of a
deferred or indeterminate turn) come only from `groups.status`'s `pending_actions`, and Retry sends
exactly that `task_id`. Disband needs an explicit confirmation step.

The surface is a bundled desktop contribution: it registers itself with the contribution registry
(P24-32, U3) and its disable switch unregisters it live (the nav entry and route go away without a
reload).

Inputs → outputs: daemon `bots.list {}` → `{bots: [{profile, label, avatar, hidden, sections,
routines, canonical: {id, tip_id} | null}]}`; `bots.open {profile}` → `{session_id, created}`;
`bots.update {profile, avatar?, hidden?, sections?}` → the bot; `profile_rows(rows)` adds
`canonical_session` to each `profiles.list` row. Renderer: `CanonicalChats.open(profile)`,
`isCanonical(sessionId)`, `withoutCanonical(rows)`; `GroupRoom` props `{rpc, roomId}`.

Files: `apps/desktop/src/bots/{BotsPage,GroupRoom,avatar}.tsx`, `canonicalChat.ts` + tests;
`src/indus/service/bots_rpc.py` + `tests/unit/service/test_bots_rpc.py`; stand-ins
`tests/unit/service/_stubs_u2.py` (R1's canonical-chat functions, P14's cron listing) and
`apps/desktop/src/test/_stubs_u2.ts` (U3's contribution registry).

Edge cases / failure modes:
- two concurrent `bots.open` for one profile → one chat (server: adopt before mint; renderer: one
  in-flight promise per profile).
- canonical chat compressed → lookup returns the tip; `isCanonical` matches root or tip.
- ambiguous lineage → the daemon refuses (R1's `AmbiguousChain`), the UI shows the error; no guess.
- unknown profile → `bots.unknown_profile`.
- avatar upload too big / wrong type → refused before any write.
- group send failure → the draft stays with its client id; Retry reuses it.
- disband without the confirm step → nothing sent.

Our design differences / decisions:
- Bot metadata lives in `<profile home>/bot.json` (avatar, hidden, sections), written atomically.
- The canonical title is R1's "Bot chat" (the reference's casing differs).
- Avatars are our own geometric generator; no reference art.

Tests we will write:
- Python: open twice concurrently → one created; open after compression → tip; list carries the
  canonical row; update validates avatar; hidden flag persists.
- TS: canonical lookup after compression opens the live tip; two concurrent clicks → one RPC;
  canonical chat absent from the sessions list; group discard needs confirmation; retry keeps the
  exact client event id / task id; disable unregisters the surface.

## Decisions for the lead
- `bots.*` methods are not declared in `indus.protocol` yet (same as R1's `groups.*`): the handler
  table is transport-free (`BotsRpc.handlers()`); declaring the methods, mounting them on the
  daemon dispatcher and adding `canonical_session` to `profiles.list` is wiring for the integrator.
- Renderer RPC goes through a narrow `RpcLike` (`request(method, params)`); the integrator passes
  the gateway connection once the methods are in the generated types.

Compare checklist (filled after the rewrite; reopened `canonical-chat.ts` and the plugin file list):
- [x] identity by (profile, canonical title) only; no pointer, no recency (test: `test_bots_rpc.py::test_open_after_compression_returns_the_live_tip`)
- [x] in-flight creation per bot (test: `canonicalChat.test.ts::two concurrent clicks create one chat`)
- [x] on-screen check matches root or tip (test: `canonicalChat.test.ts::canonical lookup after compression opens the live tip`)
- [x] canonical chats hidden from the sessions list (test: `canonicalChat.test.ts::the canonical chat is absent from the sessions list`)
- Deliberate differences: no plugin SDK import (our contribution registry, P24-32); no relay budget,
  screen sharing, skills hub picker, MCP setup or model picker inside the bots surface — those are
  general pages here.
- Not taken: group attachments, group compression, presence, slash commands in rooms, cross-
  connection bots (one connection per window).

## Status

Done (2026-10-21) against stand-ins for R1 (canonical chat, rooms) and U3 (contribution registry).
The `/bots` route is reserved in `routes.ts` (so a session can never be read as "bots") and served
through the management page slot; the contribution object (`botsContribution`) and its live switch
(`BotsSurface`) are built against the assumed registry interface (`register → unregister`) — at
integration the integrator registers it with U3's registry and the nav entry comes from there.
Wiring owed: declare `bots.*` / `groups.*` in `indus.protocol`, mount `BotsRpc(...).handlers()` with
adapters over R1's `bot_chat.find_canonical` / `ensure_canonical` (per-profile store handles), the
profile list and P14's cron listing, and add `profile_rows` to `profiles.list`. Compare (reopened
the plugin's `canonical-chat.ts` and file list): title-only identity, one in-flight creation per
bot and root-or-tip recognition match; not taken: transcript-paint wait, stale-tile focus,
attachments, presence, rounds.

## Integration (P24 integrator, 2026-10-21)

The stand-ins are gone. `indus.service.bots_rpc` gained the daemon's real ports: `RootProfiles`
(live profiles under the root), `StoreCanonicalChats` over R1's `state.bot_chat.find_canonical` /
`ensure_canonical` (each profile's own store, opened for the call with an empty environment),
`CronRoutines` (each profile's cron jobs) and `for_root(scope)`; the daemon's WebSocket serves
`bots.*` / `groups.*` (declared in `indus.protocol.bots` / `groups`, answers validated against the
declared results, `canonical: null` kept on the wire). The test drives the real chats
(`CountingChats` subclass). On the desktop `registryHost()` maps a bundled contribution onto
P24-32's registry (one `route` + one `sidebar-nav` entry, source `app`) and `main.tsx` registers the
Bots surface there; the page itself is still the workspace's `bots` slot until the shell reads those
areas (D-16.794). Still owed: `profile_rows` into `profiles.list`, the room loop (D-16.799).
