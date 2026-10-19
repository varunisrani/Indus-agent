// Every renderer timing and size limit the event handler and turn controller run on
// (research 05 §3.30; values recorded in docs/notes/P12/event-handler.md).

/** Live text reaches the screen at most this often while nothing else happens. */
export const FLUSH_IDLE_MS = 16;
/** …while the user is typing (keeps keystrokes ahead of the stream). */
export const FLUSH_TYPING_MS = 80;
/** Typing counts as stopped this long after the last key. */
export const TYPING_IDLE_MS = 250;
/** …while the transcript is being scrolled. */
export const FLUSH_SCROLL_MS = 96;
/** Scrolling counts as stopped this long after the last scroll step. */
export const SCROLL_IDLE_MS = 250;

/** The reasoning indicator stays lit this long after the last reasoning piece. */
export const REASONING_PULSE_MS = 700;
/** Reasoning text is cut back to `REASONING_KEEP_CHARS` once it grows past this. */
export const REASONING_TRIM_AT_CHARS = 80_000;
export const REASONING_KEEP_CHARS = 60_000;
/** One-line reasoning preview for the status area. */
export const THINKING_PREVIEW_CHARS = 160;

/** The live row renders at most this tail of the reply. */
export const LIVE_TAIL_MAX_CHARS = 16_000;
export const LIVE_TAIL_MAX_LINES = 240;

/** A transient status returns to "working…"/"ready" after this. */
export const STATUS_RESTORE_MS = 4_000;
/** …and after this for goal updates. */
export const GOAL_STATUS_RESTORE_MS = 6_000;
/** "interrupted" shows this long before "ready". */
export const INTERRUPT_COOLDOWN_MS = 1_500;
/** "protocol warning" shows this long. */
export const PROTOCOL_WARNING_MS = 4_000;
/** Info and warning notices disappear this long after they become visible; errors stay. */
export const NOTICE_TTL_MS = 8_000;
/** Protocol-noise previews are cut to this many characters. */
export const NOISE_PREVIEW_CHARS = 120;
/** Finished turn ids remembered so their late events are dropped. */
export const FINISHED_TURN_MEMORY = 32;
