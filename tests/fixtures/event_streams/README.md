# Recorded protocol event streams

What a session's feed delivered while the real stack (`InProcessHost` → `SessionService` → agent →
turn loop) ran a scripted fake model. Recorded by `scripts/dev/record_event_streams.py`; ids are
normalised (session `20260101_090000_rec001`, turns `turn_<n>`, requests `sr_<n>`).

One JSON object per line: an event (has `type`), a server request (has `kind`), or
`{"local": "interrupt"}` where the recording client interrupted the turn.

| File | Scenario |
|---|---|
| `plain_answer.jsonl` | a text-only reply streamed in four pieces |
| `tool_trail.jsonl` | two tool rounds (`read_file`, `terminal`) then a reply |
| `interrupt_mid_delta.jsonl` | the client interrupts after two deltas while the model stalls |
| `approval_request.jsonl` | a `terminal` call asks for approval (command redacted server-side), answered `once` |

Never edit these by hand to make a test pass: change the recorder, record again, and say why in
the task note. The TUI frontend track (P12) reuses them.
