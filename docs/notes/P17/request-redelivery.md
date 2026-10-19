# Task: Durable server-request re-delivery (P17-07) Phase: 17 Owner: wave-23 builder K2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/server_requests.py`
(all: ids, open table, re-offer through `open_requests`, not-shown declines, cancel and
settlement order), `gateway/session_server_requests.py` (all: who may settle, `resolved` teardown
for the other viewers). Research 05 §3.7 (server requests), traps R05.3, R02.5. Closed before
writing.
Evidence: none.

What it does (own words): when the agent asks a person something (approval, clarify, sudo,
secret) in a daemon-hosted session, the question goes to every attached viewer that can show that
kind. It keeps a stable id `srq-<12 hex>` and stays open in the session's request book until it is
settled. A viewer that reconnects, or that asks for missed events, is offered every still-open
request again with `replayed: true` and the same id, so its card comes back without ringing the
bell again and an answer from any copy of the card names the same request. The first answer
settles it; any later answer (another viewer, an outdated card) has no effect. Every settlement is
announced once with `request.settled {outcome: answered | withdrawn | undeliverable}` so every
other card closes.

A viewer that cannot render the kind it was sent answers `-32601` at once; the request is then
kept for the other viewers. A viewer that advertised the decline capability may answer `4404`
("not shown here"), which also leaves the request to the others. When no attached viewer is left
that could answer (all refused or declined, or none can render the kind at all), the request is
settled `undeliverable` immediately — not by waiting for a timeout — and the agent gets the
"cannot be answered here" outcome, which is distinct from a timeout or a denial (R05.3, R02.5).

Inputs → outputs:
- `RequestBook(session_id, *, emit, generation=lambda: None)` per session:
  - viewers: `attach(viewer) -> int` (re-offers open requests to it), `detach(viewer_id)`,
    `can_render(kind)`, `answerable_kinds()`;
  - lifecycle: `offer(request)` (sends to every capable viewer), `refuse(viewer_id, request_id,
    code)` for a viewer's `-32601` / `4404`, `settle(request_id, outcome, reason=None) -> bool`
    (first settlement wins), `bind_undeliverable(fn)` (the broker's hook);
  - reading: `lookup(request_id) -> OpenRequest | None`, `open_requests()`,
    `reoffer(viewer_id) -> int` (for `events.since`).
- `RequestViewer{viewer_id, kinds, can_decline, send}`; an `Offer{request, generation, replayed}`
  is what `send` gets (`to_wire()` gives the frame body).
- Broker (P04-21) changes: default ids are `srq-<12 hex>`; an optional `book` makes the broker
  ask the book whether a kind can be answered, deliver through `book.offer`, report every answer
  and withdrawal to `book.settle`, and fail the asker with `RequestUnsupported` when the book
  settles a request `undeliverable`.

Files: `src/indus/service/request_redelivery.py`, `src/indus/service/broker.py` (shared, P04
file), `tests/unit/service/test_request_redelivery.py`.

Edge cases / failure modes:
- no viewer attached, or none renders the kind → the broker refuses at once (nothing is opened).
- a viewer detaches while a request is open → the request stays open for its return (until the
  asker's own deadline); detaching is not refusing.
- a `4404` from a viewer that did not advertise the decline capability is ignored (logged); any
  other error code a viewer answers with counts as that viewer refusing.
- refusal or decline for a request that is no longer open → ignored.
- a viewer that already refused or declined a request is not re-offered it on reattach.
- a viewer's `send` raising is logged; the request stays open for the others.
- second answer → the broker's `SERVER_REQUEST_NOT_FOUND` (P04 behaviour) and no second
  `request.settled`.
- withdrawal (interrupt, timeout, turn end, session close) → `request.settled{withdrawn, reason}`
  in addition to the P04 `server_request.cancelled` event.

Our design differences / decisions:
- Names: `RequestBook`, `RequestViewer`, `Offer`, `RequestSettlement`; constants `REQUEST_ID_PREFIX`,
  `NOT_SHOWN_CODE = 4404`, `UNRENDERABLE_CODE = -32601` (the protocol's method-not-found code).
- The book is in memory per session; "durable" means it outlives viewer connections, not the
  daemon: after a daemon restart the turn waiting on it is `unknown` (R03.21) and its requests die
  with it.
- The reference answers an all-declined window request with a canned "not shown" result; we
  settle it `undeliverable` and the asker gets `RequestUnsupported` with the reason in the error
  detail, one path for both `-32601` and `4404`.
- Batch clarify per-question locks (`clarify.lock`) are out of scope here (no phase row); our
  clarify request is answered whole.
- The offer carries the generation of the turn that asked (from the fences, P17-05) so the
  client can answer `approval.respond(request_id, generation, …)`.

Tests we will write:
- reattach re-offers an open approval with the same id and `replayed: true`; `reoffer` for
  `events.since`.
- two answers → first wins, one `request.settled{answered}`.
- every viewer answers `-32601` → settled `undeliverable` at once (< 50 ms, real time), asker
  gets `RequestUnsupported`.
- declined by one viewer (4404, advertised), answered by another; 4404 without the capability
  ignored.
- withdrawal emits `request.settled{withdrawn}`; detach keeps the request open; zero viewers →
  refused up front; ids match `srq-[0-9a-f]{12}`.

## Decisions for the lead
- `request.settled` is emitted as a plain mapping event (`type: request.settled`) through the
  session's event path; P17-25 declares its model. The P04 `server_request.cancelled` event is
  kept for the in-process surfaces; the lead may retire it once every surface reads
  `request.settled`.

Compare checklist (filled after the rewrite; reopened `tui_gateway/server_requests.py`,
`gateway/session_server_requests.py`):
- [x] Ids `srq-` + 12 hex (`test_request_redelivery.py::test_request_ids_are_srq_with_twelve_hex`).
- [x] A client that cannot answer a kind is never sent it; the ask fails at once
  (`test_kind_no_viewer_renders_is_refused_up_front`).
- [x] Unanswered requests re-delivered on reattach / catch-up with the same id
  (`test_reattach_reoffers_open_approval_with_same_id`,
  `test_events_since_reoffers_still_open_requests`).
- [x] First settlement wins; a late response is dropped (`test_two_answers_first_wins_and_one_settled_event`,
  `test_book_settle_is_first_wins`).
- [x] Settled by one viewer → every other viewer's card closes (`request.settled{answered}`; the
  reference's `request.cancel{reason: resolved}`).
- [x] Withdrawal (interrupt, timeout, close) announced once (`test_withdrawal_emits_settled_withdrawn`,
  `test_timeout_withdraws_and_settles_once`).
- [x] Declines settle only when every answering viewer declined; a decline from an unknown
  viewer is recorded nowhere (`test_declined_by_one_viewer_answered_by_another`,
  `test_all_declined_is_undeliverable_not_shown`, `test_decline_without_capability_is_ignored`).
- [x] Gap found in compare: the reference settles on any error response; we ignored codes other
  than -32601/4404, so a viewer whose handler crashed could leave the agent waiting. Now any
  other error code counts as that viewer refusing (`test_other_viewer_error_counts_as_refusal`).
- Deliberate differences: `-32601` no longer ends the request at once — it is left to the other
  viewers and becomes undeliverable only when none is left (phase row, R05.3); all-declined ends as
  `undeliverable` (`RequestUnsupported` for the asker) instead of a canned "not shown" tool result;
  the settled event is `request.settled{outcome}` instead of `request.cancel{reason}`; the
  in-memory book is per session (the reference keeps one process-wide table and checks the
  owning stream on each answer).
- Not taken: batch clarify per-question locks (`clarify.lock`, no phase row); the
  `client.capabilities` advertisement verb (P17-13/14 build `RequestViewer` from what the viewer
  declared); per-actor `session:respond` authorisation (the WS layer, P17-13).

## Status

Done (2026-10-19). New files: `src/indus/service/request_redelivery.py`,
`tests/unit/service/test_request_redelivery.py`. Shared: `src/indus/service/broker.py` (P04-21:
default ids `srq-<12 hex>`, optional `book=` parameter, `_undeliverable` hook) and one assertion in
`tests/unit/service/test_broker.py` (the old `sr_` id prefix). Wiring for the lead: the daemon host
(P17-08) makes one `RequestBook(session_id, emit=lambda e: event_log.append(session_id, e),
generation=lambda: fences.running(session_id))` per hosted session, passes it to the session's
`ServerRequestBroker(book=…)`, attaches each viewer as `RequestViewer.of(viewer_id, kinds, send,
can_decline=…)`, calls `book.refuse(viewer_id, request_id, code)` when a viewer answers a request
frame with an error, and `book.reoffer(viewer_id)` from `events.since` (P17-14). In-process hosts
pass no book and keep P04 behaviour.
