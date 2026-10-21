# Task: Submission journals (P20-20) Phase: 20 Owner: wave-34 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/lib/inflight-turn-journal.ts`
(limits, storage keys, retire rules — first 140 lines), `apps/desktop/electron/prepared-submissions.ts`
(all; the main-process handlers registered from `main.ts`), `apps/desktop/src/app/session/hooks/use-prompt-actions/prepared-submissions.ts`
(header: tombstones). Research 08 §3.9 (journal numbers). Closed before writing.
Evidence: none.

What it does (own words): two journals keep a person's message from being lost or sent twice.

1. **Prepared-submission journal (main).** Before the renderer hands an input to the daemon it asks
   main to record it: one private file per input (`<userData>/prepared-submissions/<sha256 of the
   request id>.json`, 0600 in a 0700 directory, written to a temp file, fsynced, renamed). The key is
   the client's request id, the same `request_id` that `prompt.submit` carries, so retrying an entry
   can never run twice (the daemon answers the first admission for a repeated request id). The
   entry is retired only by a matching **accepted** receipt (or a definite rejection, whose text goes
   back to the composer); an **unknown** outcome (timeout, dropped link) keeps the entry, marked
   `uncertain`, and the composer offers an explicit Retry or Dismiss. Nothing is ever resent on its
   own. A crash between the write and the receipt leaves the file, which the next launch lists.
2. **In-flight turn journal (renderer, localStorage).** While a turn streams, the visible tail of
   that turn (the user message and the assistant/tool rows after it) is saved so a renderer or app
   crash does not lose streamed progress. Limits: store version 1; at most 4 MiB per store (oldest
   entries evicted); an entry at most 160 KiB serialized (larger is rejected, the previous entry is
   kept); at most 24 messages per entry (oldest dropped); a text part at most 64 KiB (clipped, marked);
   metadata at most 2 KiB (dropped when larger); at most 256 attachment refs; entries older than 7
   days are pruned; writes are trailing-edge throttled to 400 ms.
3. **Image drafts.** When a message with images is sent, its text and attachment refs are saved; the
   acknowledgement clears it. Reopening a chat whose image draft survived offers "Restore image draft"
   (fills the composer; never sends).
4. **Draft identity.** The composer's draft keeps its request id until a definite answer: an
   uncertain create/submit ACK keeps the id, so the retry is deduplicated by the daemon. The input
   id (our `request_id`) is distinct from the daemon's admission id (`turn_id` in the answer).

Inputs → outputs: `indus:journal:prepare {requestId, sessionId, payload}` → file; `indus:journal:retire
{requestId, receipt: {outcome, admissionId?}}` → `{retired}`; `indus:journal:list` → entries
(`state: prepared | uncertain`). Renderer: `InflightJournal.record/flush/read/clear/prune`,
`saveImageDraft/imageDraft/clearImageDraft`.

Files: `apps/desktop/electron/journal/preparedSubmissions.ts`, `apps/desktop/src/chat/inflightJournal.ts`,
`apps/desktop/electron/journal/preparedSubmissions.test.ts`, `apps/desktop/src/chat/inflightJournal.test.ts`;
shared: `electron/ipc/schemas.ts` (journal rows), `src/global.d.ts` (regenerated), `electron/main.ts`
(one install line).

Edge cases / failure modes:
- a corrupt entry file is moved aside (`.corrupt-<ms>`) and skipped, never blocks later sends;
- payload over 256 KiB → `too-large`, more than 200 live entries → `journal-full`: nothing is sent,
  the text stays in the composer with the error;
- retire of an unknown request id → `{retired: false}`; an `unknown` receipt marks `uncertain`;
- localStorage throws (quota, disabled) → the in-flight journal degrades silently;
- two windows: the main journal is one directory shared by all windows of the app (per-file writes,
  so windows never overwrite each other's entries).

Our design differences / decisions:
- one file per entry (the reference keeps one JSON object per origin and rewrites it whole);
- key is the client request id, not a separate admission id (our daemon dedupes on it).

Tests we will write:
- prepare then a fresh instance (restart) lists it; accepted retire removes it; unknown keeps it as
  uncertain; file 0600 / dir 0700; corrupt file quarantined; too-large refused;
- renderer: 161 KiB entry rejected; 8-day-old entry pruned; throttle 400 ms (fake clock); message,
  text, metadata, attachment caps; image draft restore never sends.

## Decisions for the lead
- Journal channel shapes changed (owner P20-20): `prepare {requestId, sessionId, payload}`,
  `retire {requestId, receipt: {outcome: accepted|rejected|unknown, admissionId?}}`, entries carry
  `requestId`, `state`, `updatedAt`. Bridge types regenerated.
- Main-journal caps (ours, not in the plan): payload ≤ 256 KiB, ≤ 200 live entries.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Atomic private write (fsync + rename, 0600/0700) (test: `preparedSubmissions.test.ts::writes
  private files (0600 in a 0700 directory)`).
- [x] Survives a restart and is offered (tests: `::survives a restart between the write and the
  receipt`, `busyActions.test.ts::offers prepared entries after a restart, for this session only`).
- [x] Unknown receipt keeps the entry (tests: `::an unknown receipt keeps the entry, marked
  uncertain`, `busyActions.test.ts::an uncertain answer keeps the entry and a retry reuses the request id`).
- [x] Torn journal quarantined, later sends not blocked (test: `::a corrupt entry is moved aside…`).
- [x] Image draft offered, never sent (tests: `inflightJournal.test.ts::an image draft survives a
  restart…`; the composer's strip only fills the editor).
- [x] Renderer limits: 4 MiB / 160 KiB / 24 / 64 KiB / 2 KiB / 256 / 7 days / 400 ms (tests:
  `inflightJournal.test.ts`).
- Deliberate differences: per-entry files; compare-and-set across windows is not needed because
  each entry has its own file and retire is keyed by request id.
- Not taken: tombstones for a removal that failed on a full disk (the entry stays and is offered
  again; the daemon's request-id dedupe makes the retry harmless).

## Status

Done (2026-10-21).
