# Task: Approvals and clarify over chat (P15-22) Phase: 15 Owner: wave-2 builder W2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/base_exec_approval.py`
(all: the shared approval wording), `gateway/run_turn_runner_approval_settle.py` (the timed-out
notice and editing the card), `gateway/run_turn_runner_clarify_delivery.py` (card send failure,
plain-text fallback, bounded wait). Research 06 §3.10/§3.11; traps R06.1, R06.6, R02.5, R05.22.
P04 broker (`service/broker.py`), P05 human approval step (`safety/approvals/human.py`), G4
admission and commands. Closed before writing.
Evidence: none.

What it does (own words): when an agent turn asks a person something through the service
(`approval` or `clarify` server request), the gateway shows it in the chat the turn came from and
turns the person's reply into the answer.

Approval card: a header saying Indus needs an OK to run a command; the command, masked on the
server (P05 redactor, forced) and wrapped (P05 `display_command`), at most 10 lines plus a
"+N more lines" count; why it was flagged; a deadline line saying that no answer means it will not
run. Buttons where the platform has them (once / this conversation / always / deny — only the
choices the request offers), else the same card with the `/approve`, `/approve session`,
`/approve always`, `/approve all`, `/deny [reason]` hint. A button card that fails to send falls
back to the text card; if nothing can be shown the request is denied at once with that reason (the
agent never waits on a prompt nobody saw).

Who may answer: only the person whose message started the turn (requester), or an admin of the
chat's tier list (P15-10, only when an admin list is configured). Button presses and typed
`/approve` / `/deny` both go through the authorization chain (P15-08) first; bots never answer. A
bare "yes", "Yes!" or "approve" is ordinary text for the agent (R06.6). `/approve` and button
presses are handled in the adapter's own await, ahead of both busy guards (R06.1), through a new
"front door" hook of the admission pipeline.

Endings, each said differently (R02.5): approved (how), denied (with reason; not run), timed out
(no answer within the window; not run), withdrawn (the reply was stopped or the session closed;
nothing ran), answered elsewhere. A button card is edited in place (dropping its buttons) when the
platform can edit, else a short notice is sent. Timeout: the request's own deadline (the broker
withdraws it; P05 reports `timeout`, not a denial) — the gateway only acts on its own when the
request carries no deadline (then `approvals.timeout`, default 300 s, and it answers deny), or
2 s after a deadline the broker missed.

Clarify: each question in turn; numbered choices (buttons where available) paged at 10 with a
"more" step, an "Other" free answer, multi-select "Done". Typed answers: single-select by number,
option text or free text; multi-select by numbers separated by commas/spaces, option texts
separated by commas, or free text. An out-of-range number gets a short hint and the question stays
open. A question without choices takes the text as is. While a clarify is open, the requester's
next plain message is the answer (intercepted before normal processing, P15-11).

Inputs → outputs: `ChatPrompts(pipeline, *, timeout=None)`; `install() -> undo` sets
`pipeline.on_request` and appends `front_door`; `deliver(source, request)`; `front_door(event) ->
bool`; `open_cards(key)`; `aclose()`. Pure helpers: `approval_card(request, scope, deadline_s)`,
`parse_choice_reply(text, choices, multi)`. Wording: `indus.service.approval_copy` (new, shared by
every surface; the gateway is its first user).

Files: `src/indus/gateway/core/prompts.py`, `src/indus/service/approval_copy.py`,
`tests/unit/gateway/core/test_prompts.py`; shared: `src/indus/gateway/core/admission.py`
(the front-door hook, ~15 lines).

Edge cases / failure modes:
- a press for a request that already ended → "no longer waiting", nothing answered.
- requester unknown (no user id on the source, not a DM) → admins only.
- two approvals open for one key: `/approve` takes the oldest, `/approve all` approves every one,
  `/deny` the oldest.
- `/approve session` when the request does not offer `session` → told which answers exist.
- a clarify answer arriving while a reply runs (busy) → still intercepted (guard 1 offers it first).
- service withdraws after our answer raced it → no second notice (one gate per card).
- a command containing a code fence cannot break out of the card's fence.

Our design differences / decisions:
- Requester rule is ours (the reference keyed answers by conversation only); shared threads would
  otherwise let anyone in the thread approve.
- Front-door hook in `AdmissionPipeline.accept` (before busy routing and mention gating) instead of
  wrapping adapter sinks; prompts run the authorization chain themselves.
- Undeliverable approval → immediate deny with reason "the approval prompt could not be shown in
  the chat"; undeliverable clarify → every question answered with a one-line "could not be asked"
  note.

Tests we will write: "yes", "Yes!", "approve" as plain text never approve (and reach the agent);
`/approve` while busy approves (both guard levels); non-requester refused and admin accepted;
button press by requester vs another user vs a bot; button card failure falls back to text;
timeout notice with fake clock (no deny answer when the broker has a deadline; deny when it has
none); withdrawn (interrupt) vs denied reported differently; long command wrapped with "+N more"
and a credential masked; clarify multi-select parsing table; clarify flow by text and by buttons
with paging and "Other".

## Decisions for the lead
- New shared wording module `service/approval_copy.py`; the CLI cards and TUI bridge still have
  their own copy — moving them onto it is a follow-up (their files are not in this slice).
- `ApprovalPrompt.text` carries the command in a fenced block; adapters format it (P15-33..35).
- Admin tier for answers only counts when an admin list is configured (`SlashPolicy.enabled`):
  with tiers off "everyone is admin" would void the requester rule.

Compare checklist (filled after the rewrite; reopened `gateway/platforms/base_exec_approval.py`,
`gateway/run_turn_runner_approval_settle.py`, `gateway/run_turn_runner_clarify_delivery.py`):
- [x] one shared wording module: what, why, and that silence means not run (`service/approval_copy.py`;
  `tests/unit/gateway/core/test_prompts.py::test_a_long_command_is_masked_wrapped_and_counted`)
- [x] timed-out card edited in place (buttons dropped) else a notice; only the timeout path posts the
  timeout notice (`::test_a_timed_out_request_says_it_did_not_run_and_is_not_a_denial`); we also
  post a distinct withdrawn notice (R02.5) (`::test_withdrawn_and_denied_are_reported_differently`)
- [x] card send failure falls back to the plain-text prompt; definitive failure releases the waiter
  (`::test_a_failed_button_card_falls_back_to_text`, `::test_a_card_that_cannot_be_shown_is_denied_at_once`)
- [x] command masked server-side and wrapped, ≤10 lines + count, fence-safe (`::test_a_long_command_is_masked_wrapped_and_counted`)
- [x] bare "yes" never approves (`::test_plain_yes_never_approves_and_reaches_the_agent`); `/approve`
  while busy answers inline (`::test_approve_while_busy_answers_inline`)
- [x] requester-or-admin rule, bots never (ours) (`::test_only_the_requester_or_an_admin_answers_in_a_shared_thread`,
  `::test_with_tiers_off_nobody_but_the_requester_answers`, `::test_button_presses`)
- [x] clarify: numbered/buttons, paging, Other, multi-select parsing (`::test_choice_reply_parsing_table`,
  `::test_clarify_by_text`, `::test_clarify_buttons_with_paging_and_other`, `::test_someone_elses_text_is_not_an_answer`)
- [x] the reference's "smart deny" card (only once / deny offered after an automatic judge said no)
  is covered by rendering only the offered choices; no separate sentence (ours).
- [x] reference's "ambiguous" (possibly posted) card sends are not modelled: our adapters report a
  definite `SendResult`; a read timeout counts as not shown (the agent is answered deny at once).
