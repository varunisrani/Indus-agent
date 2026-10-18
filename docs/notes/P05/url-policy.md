# Task: URL policy (SSRF) + website blocklist (P05-22) Phase: 05 Owner: wave-1 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/url_safety.py` (whole:
normaliser, sensitive query names, metadata floor, IP classes, fake-ip declarations, connect-time
re-resolution, redirect target helper), `tools/website_policy.py` (whole: rule normalising, shared
files, 30 s cache, host matching, fail-open), `website/docs/user-guide/security.md` (§SSRF
Protection, private-URL opt-out, fake-ip ranges). Test names and parameter tables of
`tests/tools/test_url_safety.py` for edge cases. Research `02-tools.md` (web/SSRF rows). Closed
before writing.
Evidence: none.

What it does (own words): every URL a tool is about to fetch passes one gate. The gate refuses
anything that is not plain http(s), refuses URLs that carry a credential (a secret-named query
parameter with a value, or a vendor-key-shaped value anywhere in the URL), applies the user's
website blocklist, then resolves the host and refuses it when any answer is in a non-public class
(private, loopback, link-local, CGNAT, unique-local, multicast, reserved, unspecified). The cloud
metadata floor (well-known metadata names and addresses, the whole 169.254.0.0/16 block, and the
same inside IPv4-mapped / IPv4-translated IPv6 wrappers) is refused even when the user opted in to
private URLs. Because a name can resolve differently a moment later (DNS rebinding), the httpx
transport resolves again at connect time, checks every answer again and dials the vetted address
itself while keeping the original Host header and TLS server name; each redirect hop is a new
request through the same transport, so it is checked again. Without this a prompt-injected URL can
read the cloud credentials endpoint or a LAN admin page.

Inputs → outputs: `check_url(url, scope, *, resolver=None) -> Allowed | Blocked(kind, reason)`
(sync; `check_url_async` runs it on the `io` pool); `normalize_url(text) -> str` (IRI→URI, IDNA
host, stray whitespace after `://`); `blocklist_hit(url, scope) -> BlockHit | None`;
`SafeTransport(scope, resolver=…, inner_factory=…)` (an `httpx.AsyncBaseTransport`) and
`safe_client(scope, **kw) -> httpx.AsyncClient`. Config: `security.allow_private_urls` (false),
`security.fake_ip_ranges` ([]), `security.website_blocklist {enabled: false, domains: [],
shared_files: []}`; proxy env (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, either case) read from
the scope's env.

Files: `src/indus/safety/url_policy.py`, `src/indus/safety/safe_transport.py`,
`tests/unit/safety/test_url_policy.py`, `tests/fakes/resolver.py`; shared:
`src/indus/foundation/config/schema.py` (+ `tests/unit/foundation/config/test_schema.py`).

Edge cases / failure modes:
- empty, schemeless (`example.com/x`), `ftp:`, `file:`, `http://` with no host → blocked `invalid`/`scheme`.
- literal IPs need no DNS; IPv6 literals in brackets; scope ids (`fe80::1%eth0`) stripped before parsing.
- DNS failure → blocked, except when a proxy is configured (the proxy resolves; the hostname-level
  floor still applies; a literal IP is never "deferred").
- an answer that does not parse as an address → blocked (fail closed); any unexpected error → blocked.
- resolver returns many addresses: every one is checked, at most 8 are kept for dialling.
- fake-ip declarations: unparseable entries dropped; entries overlapping loopback, RFC 1918,
  link-local, CGNAT, unique-local, IPv6 link-local or the unspecified address (so also `0.0.0.0/0`,
  `::/0`) dropped with a warning; a declared block excuses only itself, never the metadata floor.
- blocklist: rules may be bare hosts, URLs, `host/path` or `*.glob`; `www.` and trailing dots
  dropped; blank and `#` lines ignored; a bare rule matches the host and its subdomains; shared
  files are relative to the home; a missing/unreadable file warns and adds nothing; a malformed
  section fails open with a warning (one typo must not stop every web tool); cached 30 s per
  profile on the scope's clock.
- transport: unix sockets are never used (httpx only dials TCP here); a blocked answer at connect
  raises `ConnectBlocked` (an `httpx.ConnectError`), so callers see an ordinary connect failure.

Our design differences / decisions:
- One verdict object (`Allowed | Blocked`) carrying a machine `kind` instead of a bare bool, so the
  extract tool can tell the model why a URL was refused.
- Config and env are read from the call's `Scope` on every call (no process-global cache that a
  multi-profile host could leak between profiles, R02.8); the blocklist cache is keyed by profile.
- The pinned dial is done by rewriting the request URL to the vetted IP and setting httpcore's
  `sni_hostname` extension, not by swapping httpcore's private network backend; one inner
  transport per host name so a pooled connection is never reused across TLS names.
- No hard-coded "trusted host may resolve privately" exception list (the reference has one for a
  messaging vendor's media host); users who need that declare a fake-ip range or opt in.
- The secret check is a narrow name list for query parameters plus a vendor-key-shape check on
  every URL part (not the redaction query-name list, which also matches harmless `key=` facets).

Tests we will write:
- address table (seeded random sample of each blocked and allowed class, v4 and v6, mapped and
  translated wrappers); metadata floor with allow-private on; fake-ip declare/excuse/undeclarable.
- URL table: schemes, empty/hostless, secret params and key-shaped values, DNS failure with and
  without proxy, unparseable answer, many answers.
- normaliser table (IRI path, IDNA host, kept escapes, whitespace after `://` only).
- rebinding: fake resolver gives public then private → `check_url` allows, the transport blocks.
- redirect to the metadata address blocked at the hop; pinned request keeps Host and SNI.
- blocklist: glob, parent-domain match, lookalike not matched, shared file, missing file, cache
  for 30 s on a fake clock, disabled, malformed section fails open.

## Decisions for the lead
- Config keys added to `security`: `allow_private_urls`, `fake_ip_ranges`, `website_blocklist`
  (schema defaults + pinned-defaults test updated). No `INDUS_ALLOW_PRIVATE_URLS` env override
  (config only); add one if surfaces need it.
- No connect-time guard for proxied requests: when a proxy is configured the proxy resolves the
  final target, so the transport checks only the hostname-level floor and passes through.

Compare checklist (filled after the rewrite; reopened `tools/url_safety.py`, `tools/website_policy.py`,
`website/docs/user-guide/security.md` §SSRF):
- [x] scheme / hostless / empty / bad port refused (test: `tests/unit/safety/test_url_policy.py::test_unusable_urls_are_refused`)
- [x] every blocked class incl. CGNAT, benchmark, multicast, reserved, ULA, site-local (test: `::test_address_classes`)
- [x] IPv4 inside IPv6 (mapped, translated, compatible, NAT64) (test: `::test_ipv6_wrappers_are_classified_by_the_ipv4_they_carry`)
- [x] metadata floor beats allow_private, names and addresses (tests: `::test_metadata_floor_survives_allow_private`, `::test_allow_private_opens_lan_but_not_metadata`)
- [x] DNS failure fails closed; a proxy defers names, never literals or metadata names (test: `::test_dns_failure_and_proxy`)
- [x] unparseable answer and unexpected errors fail closed (tests: `::test_unreadable_answer_fails_closed`, `::test_resolver_crash_fails_closed`)
- [x] fake-ip declarations: honoured, wrapper-aware, undeclarable blocks dropped (test: `::test_fake_ip_declarations`)
- [x] secret-bearing URLs refused before any lookup; facet words pass (tests: `::test_urls_carrying_credentials_are_refused`, `::test_ordinary_query_words_pass`)
- [x] normaliser: IRI path, IDNA host, kept escapes, space after `://` only (test: `::test_normalize_url`)
- [x] connect-time re-check with every answer checked and eight kept (tests: `::test_rebinding_is_caught_at_connect`, `::test_every_answer_checked_but_eight_kept`)
- [x] dial by IP with Host and SNI kept; next address after a connect error (tests: `::test_request_is_pinned_and_keeps_host_and_sni`, `::test_next_address_tried_after_a_connect_error`)
- [x] redirect hops re-checked (tests: `::test_redirect_to_metadata_is_blocked`, `::test_redirect_to_a_named_internal_host_is_blocked`)
- [x] proxied requests untouched (test: `::test_proxy_gets_the_request_unchanged`)
- [x] blocklist rules, shared files, fail-open, 30 s cache, per profile (tests: `::test_blocklist_rules`, `::test_blocklist_shared_files`, `::test_blocklist_off_and_malformed_fail_open`, `::test_blocklist_is_cached_for_thirty_seconds`, `::test_blocklist_cache_is_per_profile`, `::test_blocklist_reaches_check_url`)
- Deliberate differences: a verdict with a reason instead of a bool; config read per call from the
  scope (no process cache, no env override, no legacy `browser.` key); the blocklist is part of
  the same gate; no trusted-host exception list; credential userinfo (`user:pass@`) and
  vendor-key-shaped values anywhere in the URL are refused; IPv4-compatible and NAT64 wrappers are
  also seen through; any glob character in a blocklist rule makes it a glob (the reference only
  treats `*.` prefixes as globs); the pinned dial rewrites the URL and sets `sni_hostname` instead
  of replacing httpcore's private network backend.
- Not taken: the floor-only helper for callers that bypass the full check (no such caller yet);
  a sync client variant (all our fetchers are async); unix-socket refusal (our inner transports
  never get a `uds`).

## Status

Done (2026-10-18). Files: `src/indus/safety/{url_policy,safe_transport}.py`,
`tests/unit/safety/test_url_policy.py`, `tests/fakes/resolver.py`. Shared:
`src/indus/foundation/config/schema.py` (`security.allow_private_urls`, `security.fake_ip_ranges`,
`security.website_blocklist`), `tests/unit/foundation/config/test_schema.py`.
