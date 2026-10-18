# Task: Approval card, clarify and sudo prompts (P06-07) Phase: 06 Owner: P06 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/cli_modal_mixin.py`
(skim: approval / clarify / sudo callbacks, timeouts, withdrawal), `tools/approval_prompt.py`
(choice semantics: once / session / always / deny, timeout distinct from deny). Research
`05-tui.md` §3.17 and traps R05.3, R05.11, R05.22. Closed before writing.
Evidence: none.

What it does (own words): when the agent asks the person something, the REPL opens a card in the
prompt area and holds every other input until it is answered.
- Approval: the command exactly as the server sent it (already redacted and wrapped there; we only
  wrap again to the terminal width, never cut, R05.22) — up to 10 lines on the card, then "N more
  lines"; the description; numbered picks in the order the request lists them: `1` once, `2` this
  session, `3` always, `4` deny (R05.11: digits, not letters). Deny asks for an optional reason
  (Enter alone skips) that goes to the model verbatim (`ApprovalAnswer.reason`). A countdown shows
  the seconds left (`deadline_s`); when it runs out the card closes with "no answer".
- Clarify: one to five questions, one at a time; numbered choices, Space-free multi-select by
  typing several digits (`1 3`), free text when the question has no choices or the person types
  words, Enter alone = skip. The answer shape is `{question_id: [answers…]}`, `[]` for skipped.
- Sudo: a masked one-line password entry; Enter sends; empty or Ctrl+C sends "no password".
- Secret: the same masked entry, answered with `value`, empty = skipped.
- Ctrl+C on any card answers the cancel form (approval: deny with reason "cancelled at the
  prompt"; clarify: everything skipped; sudo/secret: no value) — never a crash.
- `server_request.cancelled` for the open card closes it with a short note.

Inputs → outputs: `ServerRequest` model → `Card` object (`lines()`, `prompt()`, `masked`,
`on_text(text) -> Answer | None`, `on_digit(n)`, `cancel()`, `expired(now)`), the REPL sends the
answer with `host.answer_server_request`.

Files: `src/indus/cli/repl/cards.py`, `tests/unit/cli/repl/test_cards.py`.

Edge cases / failure modes:
- A second request while a card is open: queued, opened when the first closes.
- An answer the host refuses (already withdrawn): dim note, card closed.
- Digits outside the offered list: ignored; a request that offers only once/deny gets `1`/`2`.
- Very long single-line command: wrapped across lines, every character kept.

Our design differences / decisions:
- No arrow-key menu; digits and typed words (`once`, `deny`, …) are the inputs.
- Clarify questions are asked one after another rather than a tabbed panel.

Tests we will write:
- Pipe input: each digit maps to its answer; deny with a reason and without; long command wraps
  without clipping; timeout path shows "no answer"; clarify single, multi-select and skip answer
  shapes; sudo masked entry; Ctrl+C on the card answers deny-with-cancel; withdrawn card closes.

## Decisions for the lead
- A local deadline closes the card with "no answer" and sends nothing; the broker's own timeout
  withdraws the request. Clarify answers locked before the deadline are therefore not delivered
  (the reference hands partial answers back) — revisit if clarify gets per-question locks.
- Ctrl+C on an approval card answers deny with a fixed reason telling the model the person
  cancelled at the prompt.

Compare checklist (filled after the rewrite; reopened `hermes_cli/cli_modal_mixin.py`,
`tools/approval_prompt.py`):
- [x] Choice order from the request, digits 1..N (R05.11) (`test_cards.py::test_each_digit_maps_to_its_answer`,
  `::test_digits_follow_the_offered_order`, `::test_typed_words_work_and_letters_do_not`)
- [x] Deny reason optional and verbatim (`test_cards.py::test_four_denies_and_asks_for_a_reason`,
  `::test_deny_without_a_reason`)
- [x] Timeout distinct from deny: nothing answered, "no answer" shown
  (`test_cards.py::test_timeout_closes_the_card_with_no_answer`)
- [x] Withdrawal closes the card (`test_cards.py::test_a_withdrawn_request_closes_its_card`)
- [x] Redacted, wrapped, not clipped; 10 lines + count; `view` shows all (R05.22)
  (`test_cards.py::test_long_command_wraps_without_clipping`,
  `::test_very_long_commands_show_ten_lines_and_a_count_and_view_shows_all`)
- [x] Concurrent requests serialised (`test_cards.py::test_a_second_request_waits_for_the_first`)
- [x] Sudo masked, empty = no password (`test_cards.py::test_sudo_*`)
- Deliberate differences: no arrow-key selection; clarify asked one question at a time; no bell
  configuration beyond `display.bell_on_prompt`.

## Status

Done (2026-10-18).

## Integration (lead)

- Bang commands reuse `ApprovalCard` outside the prompt (`slash_bridge.approval_with`): body
  printed, picks read as lines on the terminal; Ctrl+C or end of input gives no answer (refused).
