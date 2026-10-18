# Task: SSE parser (P03-07) Phase: 03 Owner: P03-B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/chat_completion_helpers.py`
(lines 2703–2760, how a stream attempt is consumed). Framing itself written from the WHATWG HTML
Standard, "Server-sent events" section 9.2.6 (interpreting an event stream), read 2026-10-18 at
https://html.spec.whatwg.org/multipage/server-sent-events.html. Closed before writing.
Evidence: none.

What it does (own words): an incremental decoder for `text/event-stream` bodies. Bytes arrive in
arbitrary chunks; the decoder returns complete events (`event`, `data`, `id`, `retry`) and comment
lines (as `KeepAlive`) as soon as each block ends. `aiter_sse(response)` drives it over an httpx
streaming response.

Inputs → outputs: `feed(bytes)` → `list[SSEEvent | KeepAlive]`; `flush()` at EOF → the rest.

Files: `src/indus/providers/sse.py`, `tests/unit/providers/test_sse.py`.

Edge cases / failure modes:
- line endings CRLF, LF and lone CR; a CR as the last byte of a chunk followed by LF in the next
  chunk is one line end, not two.
- lines are split on bytes (CR/LF never occur inside a UTF-8 multi-byte sequence), then each
  complete line is decoded, so a character split across chunks decodes correctly; invalid bytes
  become U+FFFD.
- a UTF-8 BOM at the very start is dropped once, even when it is split across chunks.
- `data:` lines are joined with `\n`; one space after the colon is stripped; a line without a colon
  is a field with an empty value; unknown fields are ignored; `id` containing NUL is ignored.
- `retry:` only when every character is an ASCII digit.
- a block with no `data` dispatches nothing (WHATWG), but `data:` with an empty value dispatches an
  event with empty data.
- a line longer than 4 MiB (bytes, without its terminator) raises `SSELineTooLong` before it is
  buffered further.
- `data: [DONE]` is an ordinary event; transports decide.

Our design differences / decisions:
- `flush()` dispatches a pending event whose final blank line never came (a server that closes
  right after its last `data:` line). WHATWG says discard; losing the last chunk of a reply is
  worse for us. Recorded here as intended.
- `id` on an event is the last event id seen so far (WHATWG "last event ID" semantics).
- Comments are surfaced (as `KeepAlive(text)`) instead of dropped so stale detection can see them.

Tests we will write: table (CRLF, lone CR at chunk end, multi-line data, comment, missing space,
BOM, `retry: 10x`, long line); split-anywhere property (hypothesis when installed, seeded random
splits otherwise); `aiter_sse` over a MockTransport response.

## Decisions for the lead
- hypothesis is not installed in this tree (P00-11 left it out); the property test uses
  `pytest.importorskip` plus a seeded stand-in that always runs.

Compare checklist (filled after the rewrite; reopened lines 2703–2760):
- [x] consumption is chunk by chunk, keep-alive frames visible to the stale monitor (test:
  `tests/unit/providers/test_sse.py::test_comment_is_keepalive`)
- Deliberate differences: the reference parses SSE inside a vendor SDK; we own the parser.
- Not taken: nothing from the reference (framing is from the standard).

## Status

Done (2026-10-18).
