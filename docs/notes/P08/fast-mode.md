# Task: Fast mode (P08-15) Phase: 08 Owner: P08 wave 2 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/fast_mode.py` (whole file),
`hermes_cli/models.py` (`resolve_fast_mode_overrides` only). Research `01` §3.24. Vendor docs:
Anthropic fast mode (`speed: "fast"` + beta header `fast-mode-2026-02-01`, Claude API only, Opus
5 / 5.5 / 4.8; `usage.speed` reports the served speed), OpenAI priority processing
(`service_tier: "priority"`, the response's `service_tier` reports the served tier). Closed
before writing. Evidence: none.

What it does (own words): `/fast normal|fast|auto|status [--global]` chooses whether requests ask
the provider for its priority tier.

- `normal` — nothing is added (default, `agent.fast_mode: normal`);
- `fast` — every request of the session carries the tier;
- `auto` — each user turn opens a window of `agent.fast_window_s` (60 s) on the scope clock;
  requests sent inside the window carry the tier, later ones in the same turn do not;
- `status` — shows the mode, whether the current route can take it, and the time left;
- `--global` also saves `agent.fast_mode` to the profile's config.

The tier is what the route's provider profile declares: `fast_body` (request fields, e.g.
`{"speed": "fast"}`), `fast_headers` (e.g. the Anthropic beta header, merged into an existing
`anthropic-beta` list) and `fast_models` (exact model ids, any case; empty = every model). It is applied
only when the profile declares it, the model matches, and the request goes to the profile's own
host (a proxy or custom endpoint never sees the fields). Otherwise the request goes out unchanged,
`/fast` prints a notice, and the caller logs one notice per route.

The fields are layered in `BoundCaller.call` right next to `finish_request`, on the route state
read once for that request (R01.5 unchanged). Usage records the served tier: when the provider's
answer reports it we keep that; when it does not (our streamed answers carry no provider data
yet) the requested fields are recorded with `fast_requested: true`, so pricing uses fast rates
(`speed: fast`) or reports the cost unknown (`service_tier: priority` is not a standard tier) —
never standard prices for a fast request.

Inputs → outputs: mode, window, clock, route/profile → extra body fields and headers on one
request; a status line; usage raw fields.

Files: `src/indus/core/fast_mode.py`, `tests/unit/core/test_fast_mode.py`. Shared:
`src/indus/core/agent.py` (`BoundCaller.call` layering; `Agent.fast`; window opened in
`start_turn`; a rotating compaction carries the session's mode), `src/indus/cli/slash/control_cmds.py`
(`/fast` handler), `src/indus/service/commands/registry.py` (entry exists, owner P08 — unchanged
unless the args differ), `src/indus/providers/profile.py` (three declared fields),
`src/indus/providers/bundled.py` (anthropic + openai declarations),
`src/indus/foundation/config/schema.py` (`agent.fast_mode`, `agent.fast_window_s`).

Edge cases:
- unknown mode word → usage line, nothing changes; `on`/`priority` read as `fast`, `off` as normal;
- window ≤ 0 or unreadable → 60 s; a window closes exactly at its end (half-open);
- a route switch (/model, fallback) mid-window: support is re-checked per request on the new route;
- a request whose `extra_body` already sets the field keeps the caller's value (explicit wins);
- `/fast` per session lives in a process table keyed like approvals (profile home, session id);
  a rotating compaction moves it to the child id.

Our differences: no "cold" mode and no "ultrafast" tier (P3 feature, smallest useful set); no
learned "this org has no fast capacity" switch (a 429 is handled by retry/pool/fallback like any
other) — follow-up if it bites; mode per session is not stored on the session row (a resume starts
from the config default).

Tests planned: window opens per turn and expires on the fake clock; tier only inside the window
(request bodies on the fake server); `fast` mode on every request; unsupported route → notice and
an unchanged body; custom host for a declaring profile → unchanged; header merge; served tier
recorded; `/fast` handler words, `status`, `--global` save.

## Decisions for the lead
- (Superseded at integration, see below.) Anthropic was declared with `fast_models=("claude-opus-5",
  "claude-opus-4-8")` and OpenAI with `("gpt-5", "gpt-4.1", "o3", "o4-mini")` as substrings.
- Config keys `agent.fast_mode` (`normal|fast|auto`) and `agent.fast_window_s` (60).

Compare checklist (filled after the rewrite; reopened `agent/fast_mode.py`, `resolve_fast_mode_overrides`):
- [x] Window opened at each user-turn boundary, default 60 s, bad value → default
  (`tests/unit/core/test_fast_mode.py::test_window_opens_per_turn_and_expires`, `test_window_setting`).
- [x] Tier layered only while the window is open (`test_tier_only_inside_the_window`), on every
  request in `fast` (`test_fast_mode_on_every_request`).
- [x] Route gate: model and host (a proxy never sees the fields) (`test_only_declaring_profiles_on_their_own_host`).
- [x] Anthropic `speed: fast` + beta header; OpenAI `service_tier: priority` (`test_bundled_declarations`,
  `test_headers_join_an_existing_beta_list`).
- [x] Served tier recorded; a fast request never priced at standard rates
  (`test_served_tier_kept_when_reported_else_the_request_is_recorded`, `test_a_fast_request_is_never_priced_at_standard_rates`,
  `test_agent_ledger_sees_the_requested_tier`).
- Differences: no `cold` mode, no `ultrafast`, no "no fast capacity" learning from a 429 whose
  fast-limit header is 0 (follow-up), no subscription-route tier negotiation (P2 routes).

## Integration (lead)

Vendor verification (WebFetch, 2026-10-18) — the bundled declarations keep only what these pages
state:

- Anthropic — https://platform.claude.com/docs/en/build-with-claude/fast-mode (the old
  `docs.anthropic.com/.../fast-mode` URL redirects there). Confirmed: top-level request field
  `speed: "fast"`; supported models `claude-opus-5-5`, `claude-opus-5`, `claude-opus-4-8`, on the
  Claude API (Opus 5.5 also on Foundry hosted on Azure — not our host); the `fast-mode-2026-02-01`
  beta header is required for Opus 5 and 4.8 (research preview) and not needed for Opus 5.5, where
  "requests that still send the header keep working"; the response's `usage.speed` is `"fast"` or
  `"standard"`; an unsupported model or exhausted fast capacity is an error (429 / 529), not a
  silent downgrade (Opus 4.6 excepted, not declared). Fast pricing Opus 5.5 $8 / $40 per MTok —
  matches our table (`test_a_fast_request_is_never_priced_at_standard_rates`).
  → Kept `fast_body={"speed": "fast"}` and the beta header (one declaration for all three models;
  harmless on 5.5 per the page); `fast_models` is now the exact list
  `("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8")`.
- OpenAI — priority processing was renamed "Fast mode" on 2026-07-30
  (https://developers.openai.com/api/docs/changelog: "requests tagged priority will automatically
  use Fast mode"). The request field is documented in the Responses reference
  (https://developers.openai.com/api/reference/cli/resources/responses/methods/create): "include the
  `service_tier=fast` or `service_tier=priority` parameter for Responses or Chat Completions"; the
  response's `service_tier` reports the tier actually used and "will show `service_tier=priority`
  regardless of if you specify `service_tier=fast` or `priority`". The supported models are the
  Fast table of https://developers.openai.com/api/docs/pricing (22 ids: gpt-6-astra, gpt-6.1-sol,
  gpt-6-luna, gpt-6-sol, gpt-5.6-sol/-terra/-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.2,
  gpt-5.1, gpt-5, gpt-5-mini, gpt-4.1, gpt-4.1-mini, gpt-4.1-nano, gpt-4o, gpt-4o-2024-05-13,
  gpt-4o-mini, o3, o4-mini). The old guide URL (`platform.openai.com/docs/guides/priority-processing`
  → `developers.openai.com/api/docs/guides/priority-processing`) returned 404 and the help-centre
  FAQ 403 on the day, so nothing is taken from them.
  → Kept `fast_body={"service_tier": "priority"}`; `fast_models` is now exactly those 22 ids. The
  old substrings were too wide (`gpt-5` matched `gpt-5-nano`, `gpt-5.4-nano`; `o3` matched
  `o3-mini` / `o3-pro`) — none of those is in the vendor's table.
- Matching changed from substring to exact id (case-insensitive) in `fast_mode.unsupported_reason`,
  so a declaration means exactly the vendor's list; dated snapshots other than the listed one are
  not matched (a profile can list them). Test: `test_bundled_declarations` (exact ids in, nano /
  o3-mini / codex out).
- Not modelled: OpenAI's Fast mode for gpt-5.5 / gpt-5.4 is priced only below 272K context (the
  table's qualifier); `ultrafast` (a separate, access-controlled tier) is not offered (D-16.113).

Merged with P08-12/13 in `BoundCaller.call`: `finish_request` → fast tier → pool / route call, so
every pooled key carries the tier's headers (the pool only swaps the key) and `served` stamps the
outcome of the pooled call too. Intended differences: D-16.113.

Follow-ups (lead):
- streamed responses carry no served `service_tier` / `usage.speed` yet, so `served` records the
  requested tier with `fast_requested: true` — read the served value from the stream when the
  transports expose it;
- `/fast` state is per process (decision accepted); a resumed session starts from
  `agent.fast_mode`.
