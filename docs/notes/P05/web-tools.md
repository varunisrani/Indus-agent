# Task: web_search + web_extract (P05-23) Phase: 05 Owner: wave-1 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/web_tools.py` (whole:
backend selection, strict selection, search memo use, extract flow, schemas), `tools/web_tools_extract.py`
(whole: item normalising, secret refusal, order merge, provider timeout, rescue trigger, cache
keying), `tools/web_tools_truncate.py` (whole: char budget clamp, head/tail window, footer, stored
copy bound, base64 images, binary payloads), `tools/web_result_cache.py` (search memo, buckets,
single flight; extract disk cache skimmed), `tools/web_tools_rescue.py` (whole: eligibility,
annotation, never cached). Behaviour only, for the vendor adapters: `plugins/web/searxng/provider.py`,
`plugins/web/brave_free/provider.py`, `plugins/web/ddgs/provider.py`, `plugins/web/_common.py`
(result row shape). Test names of `tests/tools/test_web_result_cache.py`,
`test_web_tools_truncate.py`, `test_web_keyless_rescue.py`, `test_web_extract_robustness.py`,
`test_web_extract_timeout.py`, `test_web_providers_searxng.py`, `test_web_providers_brave_free.py`,
`test_web_tools_dict_urls.py`. Vendor wire formats checked against the vendors' public docs, not
the reference: SearXNG `GET /search?q=…&format=json` → `results[{url,title,content,score?}]`;
Brave `GET https://api.search.brave.com/res/v1/web/search?q=…&count=…` with header
`X-Subscription-Token` → `web.results[{title,url,description}]`, `count` max 20; `ddgs`
`DDGS().text(query, max_results=n)` → dicts `{title, href, body}`. Closed before writing.
Evidence: none.

What it does (own words): two tools over one provider interface. `web_search(query, limit)`
returns ranked results (title, url, snippet) from the selected provider; `web_extract(urls,
char_limit)` returns readable text of up to five pages. The provider is chosen per capability:
`web.search_backend` / `web.extract_backend`, else `web.backend`, else the first available one in
a fixed order. A provider the user named that is unknown or not usable is an error naming it,
never a quiet switch to another. Paid search answers are memoised per profile for 20 minutes;
identical concurrent searches share one provider call; when a keyed provider fails, one keyless
rescue serves that call (marked as such) and is never memoised, so the next call tries the user's
provider again. Every extract URL passes the P05-22 gate first; blocked ones get their own error
entry, the rest are fetched, and the entries come back in the order asked. Big pages are cut to a
head and a tail with a footer that names the stored full text and the `read_file` call that pages
the middle. Results are fenced as untrusted by the tool round (P04-13), not here.

Inputs → outputs: `web_search {query, limit?}` → JSON `{query, provider, results: [{position,
title, url, snippet}], rescued_from?, note?}`; `web_extract {urls: [str | {url|href}], char_limit?}`
→ JSON `{results: [{url, title, content, error}], note?}`, or an error result "inaccessible" when
nothing came back. Provider interface (`providers.py`): `name`, `keyless`, `can_search`,
`can_extract`, `available(scope)`, `async search(query, limit, scope)`, `async extract(urls,
char_limit, scope)`; `ProviderRegistry` (register/get/names). Config `web.backend`,
`web.search_backend`, `web.extract_backend` (empty = auto), `web.keyless_rescue` (true),
`web.searxng_url` (empty; env `SEARXNG_URL` also read through the scope); key
`BRAVE_SEARCH_API_KEY` from the scope's secrets, then env.

Files: `src/indus/tools/builtin/web/{__init__,providers,search,extract}.py`,
`tests/unit/tools/builtin/test_web.py`, `tests/fakes/web_provider.py`; shared:
`src/indus/tools/builtin/__init__.py` (module list), `src/indus/foundation/config/schema.py`
(`web` section), `pyproject.toml` (extra `web = ["ddgs==9.16.0"]`).

Edge cases / failure modes:
- limit: not a number → 5; clamped 1–100; provider asked for the bucket (10/20/50/100) and the
  answer sliced, so limits 3 and 7 share one memo entry.
- query blank → error; query compared case-folded with whitespace collapsed for the memo.
- memo: hit within TTL, miss after (fake clock), failures never stored, rescued answers never
  stored, ≤500 entries (oldest out), keyed by (profile, provider, query, bucket); a hit is a copy.
- single flight: two concurrent identical searches → one provider call; a cancelled leader does
  not strand the followers (they retry).
- explicit backend unknown, search-only for extract, or unavailable → error naming it.
- rescue: only after a keyed (non-keyless) provider failed, only when `web.keyless_rescue` is on
  and a keyless provider is available; rescue failure keeps the original error first.
- extract: more than 5 URLs → first 5 and a note; items that are dicts with `url`/`href` are used
  (models pass whole search hits); other items → per-entry error; `char_limit` clamped
  2,000–500,000 (default 15,000); provider timeout 120 s (scope clock) → per-URL timeout errors;
  provider returning fewer entries, extra entries or out of order → matched by URL, missing ones
  get "no result"; binary payloads (zip, gzip, PNG, PDF signature without text…) refused per page.
- truncation: whole page under the limit; else ~75 % head / 25 % tail cut on line ends; full text
  stored under `<home>/cache/web/<host>-<hash>.md` (≤2,000,000 chars with a stored-copy note);
  storage failure → footer says it could not be saved.
- inline `data:image/...;base64,` blobs → `[image: alt]` / `[image]`; http image links kept.

Our design differences / decisions:
- Providers are async (`search`/`extract` are coroutines); the sync `ddgs` call runs on the `io`
  pool with a 30 s deadline (abandoned on timeout; the reference spawns a disposable child
  process — that needs slice A's child-env builder; revisit in P13).
- A keyless `direct` extract provider is built in (fetch through the P05-22 safe client, HTML →
  text with the standard library parser), so `web_extract` works without any key. Vendor extract
  adapters arrive in P13.
- No extract disk cache (only the search memo is in the row); no managed gateway, no keyless ring.
- Secret-bearing URLs get a per-URL error (the P05-22 verdict), not a whole-call refusal.
- Result JSON shape is ours (`results`, `snippet`, `provider`).

Tests we will write:
- memo hit/expiry on a fake clock, failure not cached, bucket sharing, size bound, profile keying.
- single flight with two concurrent searches and a gate; cancellation of the leader.
- rescue served and not cached; rescue off; keyless provider failing is not rescued.
- explicit missing provider / search-only extract → error; auto order.
- extract order with a blocked URL in the middle; dict items; >5 URLs; truncation footer and
  stored file; base64 images; binary refusal; timeout; nothing returned → inaccessible.
- respx: SearXNG (score order, trailing slash, limit), Brave (header, count cap, missing key);
  `direct` extractor through the safe client with a mocked transport.

## Decisions for the lead
- Built-in `direct` extract provider (keyless) added; without it `web_extract` would have no
  provider at all in P05.
- `ddgs==9.16.0` goes in a new `web` extra; the licence check only walks `[project].dependencies`,
  so the extra is not gated by it (row 00-layout §4 says MIT).
- Config keys: `web.backend`, `web.search_backend`, `web.extract_backend`, `web.keyless_rescue`,
  `web.searxng_url`.

Compare checklist (filled after the rewrite; reopened the files above):
- [x] limit clamp, bucket, slice, positions (test: `tests/unit/tools/builtin/test_web.py::test_limit_clamped_and_bucketed`)
- [x] memo TTL on the clock, failures never stored, size bound, profile and provider in the key (tests: `::test_memo_hit_and_expiry`, `::test_memo_never_stores_failures`, `::test_memo_is_bounded_and_keyed_by_profile`, `::test_memo_keys_include_the_provider`)
- [x] single flight, shared failure, cancelled leader (tests: `::test_single_flight_shares_one_call`, `::test_single_flight_failure_reaches_every_waiter`, `::test_single_flight_survives_a_cancelled_leader`)
- [x] strict selection, never a silent switch; shared choice covers only what a provider can do (tests: `::test_explicit_unknown_or_unusable_provider_is_an_error`, `::test_explicit_search_only_provider_for_extract`, `::test_nothing_usable_names_what_to_set`)
- [x] rescue once, annotated, not cached, original error first, off switch, keyless not rescued (tests: `::test_rescue_*`, `::test_no_rescue_when_off_or_keyless`, `::test_failed_rescue_keeps_the_original_error_first`)
- [x] extract gates before the provider, per-URL errors, order restored, dict items (tests: `::test_extract_keeps_order_and_blocks_per_url`, `::test_extract_secret_and_metadata_urls_never_reach_the_provider`, `::test_extract_items_from_search_hits_and_junk`)
- [x] provider timeout, failure, short or shuffled replies, redirect target (tests: `::test_extract_provider_timeout_gives_per_url_errors`, `::test_extract_provider_failure_and_partial_answers`)
- [x] head/tail window on line ends, footer with stored path and read_file offset, stored-copy bound (tests: `::test_extract_cuts_long_pages_with_a_footer_and_stores_them`, `::test_stored_copy_is_bounded`, `::test_cut_page_without_storage_and_without_newlines`)
- [x] base64 images, binary bodies, inaccessible (tests: `::test_inline_images_become_placeholders`, `::test_binary_payloads_are_refused`, `::test_extract_secret_and_metadata_urls_never_reach_the_provider`)
- [x] SearXNG score order and trailing slash; Brave header and count cap (tests: `::test_searxng_adapter`, `::test_searxng_adapter_failures`, `::test_brave_adapter`, `::test_brave_adapter_without_web_section`)
- Deliberate differences: async providers; ddgs on the `io` pool with a 30 s deadline instead of a
  disposable child process; built-in `direct` extractor through the safe client; per-URL refusal
  of credential URLs (not the whole call); a cancelled leader lets followers retry; result shape ours.
- Not taken: extract disk cache and its exempt hosts; `web.cache_enabled` / `cache_ttl_minutes` /
  `extract_timeout` / `extract_char_limit` keys (fixed values from the row); keyless rescue for
  extract; managed gateway routes; the serialized-JSON second base64 sweep; agent-visible cache
  paths for remote backends (P11 decides how `cache/web` is seen there).

## Status

Done (2026-10-18). Files: `src/indus/tools/builtin/web/{__init__,providers,search,extract}.py`,
`tests/unit/tools/builtin/test_web.py`, `tests/fakes/web_provider.py`. Shared:
`src/indus/tools/builtin/__init__.py`, `src/indus/foundation/config/schema.py` (`web` section),
`pyproject.toml` (extra `web`), `uv.lock` (re-locked here to test; lead re-locks).
