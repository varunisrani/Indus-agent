# Task: Permission bridge and clarify (P19-11) Phase: 19 Owner: wave-30 builder A2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/permissions.py`
(options, option-id map, tool call of the card, timeout, closing the card),
`acp_adapter/edit_approval.py` (the diff card), `acp_adapter/elicitation.py` (form elicitation,
the card fallback, answers), `acp_adapter/gateway_server.py` (`_permission`, `_retire_unanswered`,
`_answer_permission`, `_answer_clarify`). Our `protocol/server_requests.py`
(`ApprovalRequest{command, description, choices, deadline_s}`, `ClarifyRequest{questions[]}`),
`service/broker.py` (deadline → withdrawn `timed_out`), `safety/approvals/human.py` and
`tools/builtin/write_file.ask_write_approval` (how the agent words timeout vs deny),
`service/remote/host.py` (`request.settled` → `server_request.cancelled`). Closed before writing.
Evidence: none.

What it does (own words): an approval or clarify request of a held session is shown in the editor
and the answer goes back through the backend (`answer(session, request_id, generation, answer)`),
the generation-bound control every surface uses.

Approval → `session/request_permission{sessionId, toolCall, options}`. The card's tool call is a
`pending` call: kind `execute`, title "description: command", content `$ command`, rawInput
`{command, description}`; when the request belongs to a file write (`write <path>`) and the
projection holds that turn's open `write_file`/`patch` call for the path, the card is a kind `edit`
call with the diff (`oldText`/`newText`). Options follow the request's `choices`:

| our choice | option id | ACP kind | name |
|---|---|---|---|
| once | `allow_once` | `allow_once` | Allow once |
| session | `allow_session` | `allow_always` (ACP has no session kind) | Allow for session |
| always | `allow_always` | `allow_always` | Allow always |
| deny | `deny` | `reject_once` | Deny |
| deny (when session or always is offered) | `deny_always` | `reject_always` | Deny always |

The answer is mapped **by option id**, never by kind (R09.12). The card's tool call is then closed
`completed` (allowed) or `failed`.

Timeout: the card waits until the request's `deadline_s` (else `approvals.timeout`, default 300 s)
on the scope's clock; then the card closes `failed` and the decision is `timeout` — nothing is
answered, the broker's own deadline withdraws the request and the agent reports "nobody answered in
time", which is not a refusal.

Not a denial: in attached mode a dismissed card (`cancelled` outcome), a transport loss or an
unknown option id answers nothing: the request stays open for another viewer, and the card key is
released so a later snapshot (reattach, load) shows a new card. In local mode the editor is the only
viewer: a dismissed card is `deny`. An answer the backend accepted keeps its key until the request
settles (`server_request.cancelled` / withdrawal), so a re-offer raises no second card. A request
withdrawn while its card is up (answered elsewhere, turn ended) cancels the card task.

Clarify (`ClarifyRequest`, one card per question, in order): a client that advertised
`elicitation.form` gets `elicitation/create{sessionId, mode:"form", message, requestedSchema:
{type:"object", properties:{answer: string | string enum | array of string enum}, required:
["answer"]}}`; `accept` → the answer (a multi-select array becomes the list of picks), `decline` /
`cancel` → an empty answer (skip). Any other client gets a permission card: one `allow_once`
option per choice (`choice-<n>`) plus Skip (`reject_once`); dismissal = skip; a free-text question
without choices offers only Skip there (no text input on a card).

Inputs → outputs: `ServerRequest` + generation → editor request → `answer` call.

Files: `src/indus/acp/permissions.py`, `src/indus/acp/elicitation.py`,
`tests/contract/acp/test_permissions.py`, `tests/contract/acp/test_elicitation.py`; shared:
`acp/agent.py` (sink → bridge).

Vendor names used (schema v1.19.0): `session/request_permission`
(`RequestPermissionRequest{sessionId, toolCall: ToolCallUpdate, options[PermissionOption{optionId,
name, kind}]}`, `RequestPermissionResponse{outcome: {outcome:"selected", optionId} |
{outcome:"cancelled"}}`), option kinds `allow_once`, `allow_always`, `reject_once`,
`reject_always`; `elicitation/create` (`CreateFormSessionElicitationRequest{sessionId, mode:"form",
message, requestedSchema}`; answers `{action: accept, content}` / `decline` / `cancel`), client
capability `elicitation.form`.

Edge cases / failure modes:
- sudo and secret requests are not offered to editors (no secret input on a card): they stay open
  for a terminal surface; the agent's own timeout applies;
- the editor errors on the request (method not found, transport) → treated as dismissal;
- two requests at once → two cards (keys by request id + generation).

Our design differences / decisions:
- Our clarify request carries several questions; each is asked in turn and the answers are sent
  together.
- Our clarify answer is a list per question, so a multi-select reaches the agent as the list of
  picks (the reference sent a JSON array string through a single-string wire).

Tests we will write:
- option-id mapping table (session by id with kind `allow_always`); unknown option attached → no
  answer; dismissed card attached → still open and answerable from another viewer;
- injected clock timeout → `timeout` decision, nothing answered;
- diff card for a write;
- elicitation: single choice, multi-select list, decline → skip; card fallback with choices.

## Decisions for the lead
- Local mode: a dismissed card is a denial (the editor is the only viewer there).
- Multi-select answers stay lists (our `ClarifyAnswer` model), not a JSON string.

Compare checklist (filled after the rewrite; reopened `permissions.py`, `edit_approval.py`, `elicitation.py`, `gateway_server.py` `_answer_permission` / `_answer_clarify` / `_retire_unanswered`):
- [x] option list by allowed choices; ids stable (test: `tests/contract/acp/test_permissions.py::test_options_follow_the_choices_and_map_by_id`, `::test_the_answer_is_mapped_by_option_id`)
- [x] closing the card completed/failed (same test)
- [x] timeout distinct from deny (test: `test_permissions.py::test_a_timed_out_card_reports_timeout_not_deny`)
- [x] retire unanswered keys; accepted keys until settled (test: `test_permissions.py::test_a_dismissed_card_is_not_a_denial_when_attached`, `::test_an_unknown_option_is_no_answer_when_attached`)
- [x] diff card for a file write (test: `test_permissions.py::test_a_file_write_card_shows_the_diff`)
- [x] elicitation schema and answers; card fallback with Skip (test: `tests/contract/acp/test_elicitation.py`)
- Deliberate differences: several clarify questions per request, asked in turn; multi-select picks
  stay a list; local mode treats a dismissed card as a denial; sudo/secret requests are not shown.
- Not taken: the auto-approve edit policies (`workspace_session`, `session`): only "ask" exists
  (D-16.29).

## Status

Done (2026-10-20), wave-30 builder A2. Shared: `acp/agent.py` (the sink hands requests and
withdrawals to the bridge), `tests/contract/acp/harness.py` (the default editor holds cards open).
