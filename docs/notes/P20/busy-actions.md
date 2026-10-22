# Task: Busy actions and queue (P20-21) Phase: 20 Owner: wave-34 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/session/hooks/use-prompt-actions/`
(file list; `queue-if-busy.ts`; submit/queue ordering skimmed), `apps/desktop/src/app/chat/composer/discard-lost-prompt.ts`,
`apps/desktop/src/app/chat/composer/queue-panel.tsx` (pause/lost header parts), grep of the auto-drain
constant in `hooks/use-composer-queue.ts`, `apps/desktop/src/api/canonical-protocol.ts` (not opened
beyond its size: our protocol is P17's). Research 08 §3.9, §3.13; R03.21. Closed before writing.
Evidence: none.

What it does (own words): while a turn runs the composer offers three actions.
- **Stop** (also Esc) interrupts exactly the running turn: `session.interrupt { session_id,
  generation }` with the generation the turn announced (`turn.started` / submit answer), never "the
  latest". When inputs wait behind it, Stop also **pauses** the queue: our daemon has no pause
  primitive, so the inputs this window queued are withdrawn from the daemon FIFO
  (`admission.cancel`) and held, in order, in a paused list the queue panel shows with Resume and
  per-item Remove. Items other surfaces queued stay on the daemon (we cannot hold their text).
- **Steer** sends guidance into the running turn (`session.steer { text, generation }`); a
  `queued: false` answer (the turn ended meanwhile) sends the text as a fresh prompt.
- **Queue** submits with `when_busy: "queue"`: the input waits in the daemon's durable FIFO, which
  survives restarts and is shared by every window and surface; the panel lists it from
  `admission.state` events (state, 1-based position).
- A turn whose outcome is **unknown** after a daemon restart (`admission.state` `unknown`, or a
  receipt's `blocked_by`) shows one Discard button (`admission.discard { admission_id, generation }`)
  and the items behind it are shown blocked; nothing is ever resubmitted automatically (R03.21).
- **Resume / auto-drain**: held items go back one at a time (`when_busy: "queue"`). A held item gets
  a fresh request id when it is held (the cancelled admission's id would only return the cancelled
  receipt) and keeps it across its drain attempts, so a retry after an uncertain answer cannot
  double-run. Each failure counts against the item; after 4 failed attempts the drain gives up and
  shows the item as failed (Resume starts a fresh round); a 5th automatic try is never made.

Every submit goes through the prepared-submission journal (P20-20): prepare → `prompt.submit
{request_id}` → retire with the receipt outcome. `SESSION_BUSY` on an idle-looking session queues.

Inputs → outputs: events (`turn.started`, `turn.done`, `admission.state`, `session.info`) and button /
key presses → `prompt.submit`, `session.interrupt`, `session.steer`, `admission.cancel`,
`admission.discard`; `TurnSnapshot` for the UI.

Files: `apps/desktop/src/chat/busyActions.ts`, `apps/desktop/src/chat/QueuePanel.tsx`,
`apps/desktop/src/chat/busyActions.test.ts`.

Edge cases / failure modes:
- Stop before the generation is known (submit answer still in flight): waits for it (bounded by the
  submit), never sends a generation-less interrupt;
- `STALE_GENERATION` on stop/discard: the turn already moved on; snapshot refreshed, no retry;
- `QUEUE_FULL`: shown, text stays in the composer;
- two windows: both see the daemon queue through events; only the window that queued an item can
  hold it on Stop.

Our design differences / decisions:
- pause is client-held (see above) because P17's queue has no pause;
- busy Enter defaults to Queue; Steer is an explicit button.

Tests we will write:
- fake daemon: stop with queue → interrupt carries the generation, own items cancelled and held
  (paused), Resume re-submits in order with their original request ids;
- unknown turn → Discard shown, queue blocked, discard carries generation; nothing resubmitted;
- drain: 4 failures then no 5th attempt; steer refused → fresh prompt;
- journal: prepare before submit, retire after; unknown outcome keeps the entry; restart offers it.

## Decisions for the lead
- Queue pause is held in the window (memory), not on the daemon; a daemon `admission.hold` (P17
  follow-up) would make it shared and durable.
- `MAX_AUTO_DRAIN_ATTEMPTS = 4`.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Stop carries the exact generation (tests: `busyActions.test.ts::stop carries the generation of
  the running turn`, `::stop waits for the submit answer instead of interrupting blindly`).
- [x] Stop with a queue pauses it (test: `::stop with a queue pauses it and Resume sends the items
  back in order`).
- [x] Lost turn → single Discard, queue blocked, never resubmitted (tests: `::an unknown turn shows
  one Discard…`, `::a lost turn without a generation asks the receipt for it`).
- [x] Auto-drain gives up after 4 attempts (test: `::the fifth drain failure is not attempted`).
- [x] Steer to the running generation; refused steer becomes a prompt (`::steer goes to the running…`).
- Deliberate differences: the reference's gateway pauses its own queue; ours holds own items client-side.
- Not taken: editing a queued item in place (`replace_queued`) and steering a queued item into the turn
  (no protocol method yet).

## Status

Done (2026-10-21).

Integration (wave-34 merge, D-16.682): `sharedActions(connection, sessionId, make)` keeps one
`ChatActions` per session and connection (the composer's fresh-draft send, the composer showing the
session and the transcript's Retry use it); `recoverPrepared()` skips inputs this object is sending or
has settled — the wired run showed the first message of a new chat as "Not confirmed" while it ran
(test `busyActions.test.ts::listing the journal mid-submit never offers the input being sent`).

## Wave 47 (builder a, 2026-10-22): Stop/Steer stuck after a turn ended

Status: done (fail-first tests below fail on the snapshot base and pass with the fix).

Read: no reference file and no Evidence (a bug in our own busy state, found while capturing
screenshots: the composer kept Stop/Steer for minutes after the model went idle; a window reload
cleared it).

Cause, two parts:
- **Two ids for one turn.** The submit answer and `admission.state` name an input by its admission
  id; `turn.started` / `turn.done` carry the daemon's own turn id (`agent.start_turn` mints it). The
  busy state stored whichever came last as `turnId` and `turn.done` ended the busy state only when its
  id matched. When `turn.started` arrived before the submit answer (or before `admission.state
  started`), `turnId` became the admission id and `turn.done` was ignored; when the answer arrived
  after `turn.done`, it switched the busy state back on. Only a later `session.info { running:
  false }` (a reattach, i.e. a reload) cleared it. The old tests hid this by using the admission id
  as the turn id in `turn.started`.
- **Nobody listening.** `sharedActions` objects were fed events only while a composer showing their
  session was mounted. The fresh draft's first send, quick entry and Retry use them with no such
  composer, so a turn that ended before the session's composer mounted left Stop up.

Fix (`src/chat/busyActions.ts`): busy state is kept by generation, the one number both ids share. The
object remembers the daemon's turn ids seen in `turn.started` with their generation (the latest 16)
and the newest generation whose end it saw. `turn.done` ends the busy state unless a later turn that
already started is running; a submit answer or `admission.state started` for a generation that
already ended never shows Stop again (its `turnId` is still the turn's own id when known, so the
optimistic row binds to the turn the transcript holds). `sharedActions` subscribes the object to its
session's events for its whole life; the composer subscribes only actions handed in from outside.

Tests: `src/chat/busyActions.test.ts` "busy state ends with the turn, whatever order the answers
arrive in" (answer after `turn.started`; answer after `turn.done`; admission started after
`turn.started` and after `turn.done`; `turn.done` of an unseen turn; an earlier turn's end does not
stop the next; shared actions hear events with no composer mounted).

### Decisions for the lead
- W47-a-1 (D-16.1030): busy state follows the generation, not the turn id; the admission id is kept only as a
  fallback `turnId` until `turn.started` names the turn. No protocol change.
- W47-a-2 (D-16.1031): `sharedActions` holds one permanent session-event listener per (connection, session);
  the per-composer subscription remains only for actions passed in as a prop (tests, embeds).
