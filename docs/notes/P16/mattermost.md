# Task: Mattermost adapter (P16-08) Phase: 16 Owner: wave-23 builder E2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/mattermost/adapter.py`
(all: REST helper, thread-root resolution, connect, send / edit / typing, file upload and download,
WebSocket loop with backoff, channel gating, posted-event normalisation, standalone send),
`mattermost/plugin.yaml`, `mattermost/gateway_env.py`; test names only in
`tests/gateway/test_mattermost*.py`. Research 06 §3.3 (row Mattermost), §3.5. Evidence: none.

What it does (own words): connects Indus Agent to a Mattermost server (self-hosted or cloud) as a bot
or personal-access-token user. `GET /api/v4/users/me` proves the token and gives the bot's id and
username. Inbound, the server's WebSocket (`/api/v4/websocket`) pushes `posted` events; each human
post becomes one `InboundEvent`: channel types D → DM, G/P → group, O → channel; `root_id` makes a
thread; mentions (the event's `mentions` list or `@username` in the text) mark the event
`mentioned` (the `@username` is cut from the text); attached files become attachments the intake
downloads through `fetch_media`. Our own posts, system posts, edits and posts outside
`allowed_channels` (DMs exempt) are ignored; a lost WebSocket reconnects after 2 s, doubling to
60 s; a refused handshake (401/403) stops the loop as fatal. Outbound, posts go to `/api/v4/posts`
(threads by `root_id`), long text is split at 4,000 characters, edits patch the post, deletes
delete it, typing posts to the typing endpoint, files are uploaded to `/api/v4/files` and attached
by id. `@channel`, `@all` and `@here` written by the agent are defused so a reply never pings a
whole channel.

Inputs → outputs: `AdapterConfig` (`MATTERMOST_TOKEN` secret, `MATTERMOST_URL` secret/env or
`platforms.mattermost.url`, `platforms.mattermost.*` options) → a connected adapter; WebSocket JSON
→ `InboundEvent`s; sends / edits / deletes / media / typing → REST calls → `SendResult`s.

Files: `src/indus/gateway/adapters/mattermost/__init__.py`, `.../mattermost/adapter.py`, new
`.../mattermost/client.py` (REST client over httpx, error readings, route table, WebSocket opener),
`tests/fakes/platform/mattermost_wire.py`, `tests/unit/gateway/adapters/mattermost/test_adapter.py`;
shared: `pyproject.toml` (extra `mattermost = ["httpx-ws==0.9.0"]`), `THIRD_PARTY_NOTICES.md`
(httpx-ws and wsproto rows gain the extra), `tests/contract/gateway/conftest.py`.

Vendor API: Mattermost REST API v4 + WebSocket API, server ≥ 9. Routes (`client.ROUTES`):
`GET users/me`, `POST posts`, `PUT posts/{id}/patch`, `DELETE posts/{id}`, `GET posts/{id}`,
`POST users/{id}/typing`, `GET channels/{id}`, `POST files`, `GET files/{id}`, `GET users/me/teams`,
`GET users/me/teams/{team}/channels`, `POST reactions`, `DELETE users/{id}/posts/{post}/reactions/
{emoji}`; WebSocket `GET websocket` (events `hello`, `posted`; others ignored). WebSocket client:
`httpx-ws` 0.9.0 (MIT, uploaded 2026-03-28; needs `wsproto` 1.3.2, MIT) — both already in the lock
through another extra, so the new extra adds no package.

Capability flags (research 06 Q4): `dm_is_private` true (a D channel has two members),
`owns_access_policy` false (the core chain decides; the adapter only filters `allowed_channels`),
`auth_upstream` false, `supports_edit` true, `supports_delete` true, `supports_buttons` false
(interactive buttons need an integration callback URL; approvals go as text), `push_capable` true,
`splits_natively` true, cap 4,000 code points, markup `markdown`.

Edge cases / failure modes:
- 429 → rate limited with `Retry-After` (or `X-Ratelimit-Reset`) seconds.
- 401 at connect → fatal; on a send → final failure (never a dead chat). 403 → forbidden.
- 404 on a channel → chat not found; an invalid `root_id` → thread not found (chat alive); a missing
  post on edit → message not found; message too long → too long; invalid body → bad format;
  502/503/504 → connection-level; read timeouts never retried.
- A reply in thread mode resolves the true root first (a reply's own id is not a valid root).
- WebSocket: hello / unknown events ignored; malformed JSON skipped; redelivered post ids deduped
  (last 1,000).
- Send-only open: connect is `users/me` only; the WebSocket opens only when an inbound sink is
  installed.
- Missing `httpx-ws` (extra not installed): the listener fails as fatal with the install hint; sends
  still work.

Our design differences / decisions:
- httpx for REST (core dependency) and httpx-ws for the socket instead of aiohttp (no new package).
- The adapter never sends the token in a message body (header auth on the upgrade request) — the
  reference also sent an `authentication_challenge`.
- Mass mentions are defused in the text (zero-width space after `@`) instead of a post property.
- Edits (`post_edited`) are ignored like the reference; reactions are offered as helpers for the
  processing-state seam (no core caller yet).

Tests we will write:
- contract suite over the wire fake;
- thread replies keyed by the root id (inbound and outbound `root_id`, root resolution in thread
  mode);
- channel not in `allowed_channels` ignored, DMs exempt;
- mention detection and stripping; own, system and duplicate posts ignored; bot posts marked;
- WebSocket reconnect 2/4/8… s capped at 60 s (fake clock); handshake 401 fatal;
- send-only open does no WebSocket; error readings; mass mentions defused; image markdown → URL;
- media upload + post with file id; fetcher streams with a cap; channel directory listing;
- token never in logs, errors or repr; client route table served by the wire fake.

## Decisions for the lead
- Proposed registry row (P15 core file, not edited): `_chat_entry("mattermost", "Mattermost",
  sdk=("httpx_ws",), extra_name="mattermost", required_env=("MATTERMOST_TOKEN", "MATTERMOST_URL"),
  max_len=4000, hint=...)`; `config.BUILTIN_CREDENTIALS["mattermost"] = ("MATTERMOST_TOKEN",)`.
- `reply_mode: thread` (reference name and default `off`) makes a channel's top-level posts
  thread roots and threads replies under the user's post; `off` answers flat unless the user wrote
  in a thread.
- No WebSocket jitter by default in tests (rng injectable); production uses ±20 %.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/mattermost/adapter.py`):
- [x] `users/me` proves the token; 401 fatal, other failures retryable
  (`test_adapter.py::test_connect_failures_are_classified`).
- [x] `posted` only; own, system and redelivered posts dropped; bot posts marked; leading-space
  slash command (`::test_mentions_bots_system_posts_and_repeats`).
- [x] Mentions from the event's `mentions` list, `@username` and `@<bot id>`, removed from the text
  (gap found: the reference also matches `@<bot id>` — added).
- [x] `allowed_channels` checked first, DMs exempt (`::test_channel_not_in_the_allowlist_is_ignored`,
  `::test_allowed_channels_come_from_config_and_env`); `require_mention` /
  `free_response_channels` are the core's mention policy (same keys).
- [x] Threads by `root_id`; `reply_mode: thread` makes top-level posts roots and resolves a reply's
  true root (`::test_thread_replies_are_keyed_by_the_root_id`, `::test_channel_types_and_thread_mode`,
  `::test_reply_mode_thread_threads_replies_under_the_true_root`).
- [x] Channel types D/G/P/O (`::test_channel_types_and_thread_mode`, `::test_channel_info_and_directory`).
- [x] Files: image → photo, audio → voice (video → video, ours), downloads with auth and a cap
  (`::test_media_posts_and_the_fetcher`); upload then post with `file_ids`
  (`::test_media_upload_then_post`).
- [x] 4,000-character pieces (`::test_long_text_is_split_at_4000_characters`); image markdown →
  URL (`::test_outbound_text_rules`).
- [x] Edit by `PUT posts/{id}/patch`, delete, typing with the thread as parent, reactions
  (`::test_edit_delete_typing_and_reactions`).
- [x] Socket backoff 2 → 60 s (jitter injectable), a refused upgrade is fatal
  (`::test_the_socket_reopens_with_backoff_capped_at_sixty_seconds`, `::test_a_refused_upgrade_is_fatal`);
  the real httpx-ws path against an in-process ASGI server (`::test_the_real_opener_speaks_to_an_asgi_server`,
  skipped without the extra).
- [x] Standalone send = our send-only open (`::test_send_only_open_never_opens_the_socket`,
  `::test_indus_send_over_the_wire_does_no_inbound_work`).
- [x] Contract suite (`tests/contract/gateway`, case `mattermost`): all rules, no real network.
- Deliberate differences: token in the upgrade request's header instead of an
  `authentication_challenge` message; mass mentions defused in the text instead of a post property;
  attachments downloaded by the intake on demand (the reference downloaded every file at once);
  video files are video events; a failed send reports its classified reading instead of an empty
  answer.
- Not taken (recorded): the flat-channel fallback when a thread root is broken for a final reply
  (the failure is reported as "thread not found"; the sender decides); batching several images
  into one post (one file per post); sending an image from a URL (the core's media policy resolves
  remote files first).

## Status

Done (2026-10-19). Shared files touched: `pyproject.toml` (extra `mattermost`),
`THIRD_PARTY_NOTICES.md` (httpx-ws and wsproto rows name the new extra), `tests/contract/gateway/
conftest.py` (case registration), `src/indus/foundation/config/schema.py` +
`tests/unit/foundation/config/test_schema.py` (`platforms.mattermost` row). `uv.lock` left as it
was (tested with a re-lock: the diff is only the extra's metadata, no package added or moved).
Wiring left for the lead (P15 core files): the registry row and credential entry above;
`adapter.register_fetchers(intake)`, `adapter.lost_handler`, `processing_started` /
`processing_finished` as for Matrix.

Landed (integrator, 2026-10-20; D-16.386..391, D-16.393): `lost_handler` removed — a refused socket
upgrade calls `report_fatal("mattermost_auth", …)` and a missing httpx-ws `report_fatal(
"missing_dependency", …)`; `register_fetchers(seat)` returns the undo; `send_only` from
`AdapterConfig.send_only` keeps the socket closed even with a sink; registry row (probe `httpx_ws`,
extra `mattermost`) and `BUILTIN_CREDENTIALS["mattermost"]` added; `uv.lock` re-locked (extra
metadata only); the contract case moved to `tests/contract/gateway/mattermost_case.py`.
