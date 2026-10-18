# Task: Hosts and API-mode detection (P03-04) Phase: 03 Owner: P03-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `utils.py` lines 735–796 (hostname,
path, origin and host-match helpers), `hermes_cli/runtime_provider.py` `_detect_api_mode_for_url`
(the URL rules only). Closed before writing.
Evidence: none.

What it does (own words): every decision about where a key may go and which wire format a URL
speaks is made on a parsed URL, never on substrings. `parse_base_url` refuses anything ambiguous
(no scheme, userinfo, whitespace, backslashes, fragments, bad ports, empty hosts) and returns the
scheme, a normalised host (lowercase, IDNA/punycode, no trailing dot), the port and the path.
`host_matches(url, domain)` is exact host equality; a dot-boundary subdomain only when the caller
passes `allow_subdomains=True` for an explicitly allowed suffix. `is_loopback` covers
127.0.0.0/8, `::1` (and its IPv4-mapped form) and `localhost`; `is_private_network` adds RFC 1918,
link-local and unique-local ranges for the P03-13 local-timeout rule. `api_mode_from_url` returns
`responses` for the official OpenAI hosts (a hint; the route falls back until P08),
`anthropic_messages` for `api.anthropic.com` and for a path ending `/anthropic` or `/anthropic/v1`,
else `chat_completions`.

Inputs → outputs: URL text → `ParsedURL` or `InvalidBaseURL` (code `provider.invalid_base_url`);
URL + domain → bool; URL → api mode string.

Files: `src/indus/providers/hosts.py`, `tests/unit/providers/test_hosts.py`.

Edge cases / failure modes:
- Spoof shapes: `api.openai.com.evil.example`, `evilapi.openai.com`, `evil.example/api.openai.com`,
  `api.openai.com@evil.example`, `evil.example#@api.openai.com`, `evil.example\@api.openai.com`,
  `api.openai.com:443@evil.example`, unicode lookalikes (punycode differs), `api-openai.com`,
  `api.openai.co`, `openai.com` for `api.openai.com`, `http:/api.openai.com`, query-string tricks.
- Uppercase host and trailing dot normalised; IPv6 literals kept without brackets.
- `host_matches` never raises: an unparsable URL simply does not match.

Our design differences / decisions:
- Strict parser: the reference tolerates scheme-less hosts; we refuse them (a base URL in config
  must say http or https).
- Subdomain matching is opt-in per call, so key scoping is exact by default.
- `api.anthropic.com` is included in the URL rules (the reference keeps it in a host table).

Tests we will write: parse table (normalisation, rejects); spoof table ≥ 15 rows all false for
`api.openai.com`; positive rows; `allow_subdomains`; loopback/private tables incl.
`http://[::1]:8080/v1`; API-mode table incl. `https://api.minimax.io/anthropic`; a seeded random
property check (hypothesis is not a dependency yet): whenever `host_matches(url, d)` is true the
parsed host equals `d` or ends with `"." + d`.

## Decisions for the lead
- Hypothesis is not installed in this tree (P00-11 listed it but it is not in the dev group), so
  the property test uses a seeded `random.Random` generator over spoof fragments (2,000 cases).
  Swap to `@given` when hypothesis lands.

Compare checklist (filled after the rewrite; reopened `utils.py` 735–796 and the URL rules):
- [x] lookalike subdomain and path spoof rejected (test:
  `tests/unit/providers/test_hosts.py::test_spoof_table_never_matches`)
- [x] exact host or dot-boundary subdomain only (test: `::test_property_match_implies_host_or_subdomain`)
- [x] official OpenAI hosts → responses hint; `/anthropic` and `/anthropic/v1` → messages (test:
  `::test_api_mode_from_url`)
- Deliberate differences: no scheme-less hosts; subdomains opt-in; userinfo/fragments/backslashes
  refused outright; IDNA normalisation (the reference only lowercases).
- Not taken: the vendor `/coding` route rule and host-mandated Responses for other vendors (P08
  adds Responses); origin (scheme + port) comparison lives in `route` as the downgrade check.

## Status

Done (2026-10-18).

## Integration (lead)

- `is_private_network` is now also the base of `timeouts.is_local_endpoint` (which adds the
  unspecified address, CGNAT and local-only suffixes on top).
