// The transcript-once oracle (P20-18, shared with the P20-40 Electron suite). Inputs are plain
// message lists so the same checks run over the reducer's items (unit tests) and over the DOM
// (end-to-end): what the daemon stored, what is on screen, and what the backend streamed.

export interface ShownMessage {
  readonly role: "user" | "assistant";
  readonly text: string;
}

export interface StoredMessage {
  readonly role: "user" | "assistant" | "tool";
  readonly text: string;
}

export interface StreamRecord {
  /** Concatenated `message.delta` text the provider streamed for the turn. */
  readonly text: string;
  /** Concatenated `reasoning.delta` text. */
  readonly reasoning: string;
  /** The `message.complete` text. */
  readonly final: string;
}

const norm = (text: string): string => text.replace(/\s+/g, " ").trim();

/** Stored user/assistant rows with text, in order (tool rows are not messages). */
export function persistedMessages(rows: readonly StoredMessage[]): ShownMessage[] {
  return rows
    .filter((row): row is ShownMessage => row.role !== "tool" && norm(row.text) !== "")
    .map((row) => ({ role: row.role, text: row.text }));
}

/**
 * Every persisted message is shown exactly once and in order, and nothing that is not
 * persisted is shown. Returns the violations (empty when the screen is right).
 */
export function checkTranscriptOnce(
  persisted: readonly ShownMessage[],
  shown: readonly ShownMessage[],
): string[] {
  const problems: string[] = [];
  const want = persisted.map((m) => `${m.role}:${norm(m.text)}`);
  const got = shown.map((m) => `${m.role}:${norm(m.text)}`);
  for (const key of new Set(want)) {
    const expected = want.filter((k) => k === key).length;
    const seen = got.filter((k) => k === key).length;
    if (seen !== expected) problems.push(`${key} shown ${seen}× (stored ${expected}×)`);
  }
  for (const key of got) {
    if (!want.includes(key)) problems.push(`${key} shown but not stored`);
  }
  if (problems.length === 0 && want.join("\n") !== got.join("\n")) {
    problems.push(
      `order differs:\n  stored ${JSON.stringify(want)}\n  shown  ${JSON.stringify(got)}`,
    );
  }
  return problems;
}

/** No message is shown twice in any intermediate frame, and stored ones never swap places. */
export function checkFrames(frames: readonly (readonly ShownMessage[])[]): string[] {
  const problems: string[] = [];
  frames.forEach((frame, index) => {
    const keys = frame.map((m) => `${m.role}:${norm(m.text)}`).filter((k) => !k.endsWith(":"));
    const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);
    if (dupes.length > 0) problems.push(`frame ${index}: ${[...new Set(dupes)].join(", ")} twice`);
    const users = frame.filter((m) => m.role === "user").map((m) => norm(m.text));
    const previous = index > 0 ? (frames[index - 1] ?? []) : [];
    const before = previous.filter((m) => m.role === "user").map((m) => norm(m.text));
    const common = users.filter((u) => before.includes(u));
    const order = before.filter((u) => common.includes(u));
    if (common.join("\n") !== order.join("\n"))
      problems.push(`frame ${index}: user rows reordered`);
  });
  return problems;
}

/** What a turn's deltas add up to equals what was streamed; the final equals the completion. */
export function checkStreamIntegrity(
  events: readonly Record<string, unknown>[],
  streamed: Readonly<Record<string, StreamRecord>>,
): string[] {
  const problems: string[] = [];
  const seen = new Set<string>();
  const text = new Map<string, string>();
  const reasoning = new Map<string, string>();
  const finals = new Map<string, string>();
  for (const event of events) {
    const key = `${String(event.replay_epoch ?? "")}:${String(event.seq ?? "")}`;
    if (event.seq !== undefined && seen.has(key)) continue;
    seen.add(key);
    const turn = String(event.turn_id ?? "");
    if (event.type === "message.delta") text.set(turn, (text.get(turn) ?? "") + String(event.text));
    if (event.type === "reasoning.delta")
      reasoning.set(turn, (reasoning.get(turn) ?? "") + String(event.text));
    if (event.type === "message.complete") finals.set(turn, String(event.text));
  }
  for (const [turn, record] of Object.entries(streamed)) {
    if ((text.get(turn) ?? "") !== record.text) problems.push(`${turn}: deltas differ from stream`);
    if ((reasoning.get(turn) ?? "") !== record.reasoning)
      problems.push(`${turn}: reasoning differs from stream`);
    if ((finals.get(turn) ?? "") !== record.final) problems.push(`${turn}: final differs`);
  }
  return problems;
}
