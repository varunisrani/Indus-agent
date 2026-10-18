# Task: HTTP client factory (P03-08) Phase: 03 Owner: P03-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/chat_completion_helpers.py`
lines 427–700 (which timeout knobs exist), the stream-timeout helper around line 2900 and
`hermes_cli/timeouts.py` (per-model / per-provider request timeout lookup) — knobs only. Closed
before writing.
Evidence: none.

What it does (own words): one place builds the `httpx.AsyncClient` every transport uses.
Timeouts: connect 30 s, read 120 s, write 1,800 s, pool 30 s. The request timeout comes from
`providers.<name>.models.<id>.timeout_seconds`, else `providers.<name>.request_timeout_seconds`,
else `INDUS_API_TIMEOUT`, else 1,800 s; a configured (not env/default) request timeout is also the
read timeout. Otherwise read is raised to the request timeout for loopback endpoints (local servers
prefill for minutes) and to the stale timeout when P03-13 hands a larger one for reasoning streams.
Headers: our `User-Agent: indus/<version>` plus the profile's own default headers, nothing else
(no vendor identity headers, R01.12). `network.force_ipv4` binds the local address `0.0.0.0`.
HTTP/2 off, redirects not followed (a key never follows a redirect to another host). A `transport`
argument lets fakes and captures plug in. Clients are cached per (base URL, key fingerprint,
timeouts, IPv4 flag, transport, event loop) and closed by `close_all()`. `open_stream(client,
request)` returns a `StreamHandle` whose `aclose()` aborts the response (closing the connection
instead of draining it) so a cancelled turn frees the socket at once (R01.4).

Inputs → outputs: route + timeouts (+ transport) → `httpx.AsyncClient`; client + request →
`StreamHandle` (async iterator over bytes, async context manager).

Files: `src/indus/providers/http.py`, `tests/unit/providers/test_http.py`; shared: `pyproject.toml`
(`anyio==4.15.1`, `httpx==0.28.1`, dev `respx==0.23.1`), `THIRD_PARTY_NOTICES.md`.

Edge cases / failure modes:
- Non-numeric or non-positive timeout settings are ignored with a debug log.
- `aclose()` is idempotent and never raises (errors while aborting are logged).
- Cancelling a task blocked in `aiter_bytes()` closes the stream; the measured test asserts
  < 200 ms (`slow`).
- A cached client whose event loop has closed is not reused (loop id in the key).

Our design differences / decisions:
- asyncio + httpx instead of SDK clients on worker threads (D-04.1, D-04.5); no foreign-thread
  socket shutdown tricks — cancellation closes the stream in the task.
- `force_ipv4` is passed explicitly (`client_for(..., force_ipv4=...)`) instead of a process-wide
  socket patch; `timeouts_for(route, scope, stale_timeout=...)` reads the config knobs.

Tests we will write: timeout precedence table; loopback and stale raise of read; UA exact;
respx route asserting no `x-*` identity headers and no foreign product tokens; `force_ipv4`
passes `local_address`; clients reused per key, distinct per key, closed by `close_all`; cancel a
task on a never-ending MockTransport stream → `aclose` observed and task done < 200 ms (`slow`);
redirects not followed.

## Decisions for the lead
- `anyio` is pinned directly in `[project].dependencies` next to httpx (the phase row says
  "+ anyio"); `sniffio` is no longer pulled by anyio 4.15, so it is not in the notices table.

Compare checklist (filled after the rewrite; reopened the timeout helpers):
- [x] knobs: per-model `timeout_seconds` > provider `request_timeout_seconds` > env (test:
  `tests/unit/providers/test_http.py::test_request_timeout_precedence`)
- [x] read raised for local endpoints and to the stale timeout (test: `::test_read_timeout_raises`)
- [x] connect/pool cover only the handshake (30 s) (test: `::test_default_timeouts`)
- Deliberate differences: no SDK clients; cancellation closes the stream in-task; per-client
  cache keyed by key fingerprint; redirects off.
- Not taken: the Codex hard-timeout ceiling (no Codex route), keepalive-client tricks for
  non-streaming inline calls (P03-14 owns call timing).

## Status

Done (2026-10-18).
