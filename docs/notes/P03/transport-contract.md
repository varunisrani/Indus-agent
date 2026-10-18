# Task: Transport contract suite (P03-12) Phase: 03 Owner: P03-B1 builder

Read: none (our own contract).
Evidence: none.

What it does (own words): one parametrised suite every transport must pass. For each transport and
each capture or fake-model script it streams the reply through the real transport and checks that
the events reduce (`assemble`) to a sane `NormalizedResponse`: text equals the joined deltas, tool
calls have ids, names and JSON-object arguments, usage is present when the server sent it, the
finish reason is mapped, keep-alives carry no content. It also checks that cancelling a consumer
mid-stream closes the HTTP response, and that a 429 with `Retry-After: 7` surfaces as
`ProviderHTTPError(status=429, retry_after=7.0)`. P08 adds its transports to the parameter list.

Files: `tests/contract/providers/__init__.py`, `tests/contract/providers/test_transport_contract.py`,
`tests/contract/providers/_stubs_p03b2.py` (test-only stand-in for the P03-18 fake model server,
used only while `tests/fakes/model/server.py` is missing).

Our design differences / decisions:
- The cancellation check runs through `transports.open_call` + `stream_events`; the P03-08 stream
  handle plugs in through `open_call(send=...)` once it lands.
- Fake-model scripts use only the P00-11 vocabulary (Text, Reasoning, ToolCall, Usage, Finish), six
  per transport.

## Decisions for the lead
- When P03-18 lands, the stub module imports the real `FakeModelServer` first; delete the stub's
  fallback once both slices are merged.

Compare checklist: none (no reference file for this task).

## Status

Done (2026-10-18).

## Integration (lead)

- `tests/contract/providers/_stubs_p03b2.py` deleted: the contract suite runs on the real
  `tests.fakes.model.server.FakeModelServer` and the real `RuntimeRoute` (`make_route` in
  `tests/unit/providers/_support.py`). All contract tests pass unchanged.
