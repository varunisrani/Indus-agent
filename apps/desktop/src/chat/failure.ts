// Which layer a failed turn broke in, and what the error card offers for it (P20-18). Our
// protocol carries the turn's exit reason and a failure sentence, not a structured surface, so
// the layer is read from both; the connection layer ("daemon") comes from the socket state.

export const FAILURE_LAYERS = [
  "provider",
  "endpoint",
  "streaming",
  "auth",
  "daemon",
  "runtime",
  "disk",
] as const;

export type FailureLayer = (typeof FAILURE_LAYERS)[number];

export type FailureAction =
  | "retry"
  | "choose-model"
  | "open-settings"
  | "sign-in"
  | "reconnect"
  | "open-logs"
  | "new-session";

/** Exit reasons that end a turn without an answer even when no failure text came with them. */
export const FAILED_EXITS: ReadonlySet<string> = new Set([
  "budget_exhausted",
  "max_iterations",
  "invalid_tool_strikes",
  "persistence_failed",
  "provider_failed",
  "compression_exhausted",
  "guardrail_halt",
]);

const RULES: ReadonlyArray<readonly [FailureLayer, RegExp]> = [
  [
    "disk",
    /\b(disk|no space|enospc|read-only file|quota exceeded on device|database is locked)\b/i,
  ],
  [
    "auth",
    /\b(401|403|unauthori[sz]ed|forbidden|api[ _-]?key|invalid key|credential|sign[ -]?in|oauth|token expired)\b/i,
  ],
  [
    "streaming",
    /\b(stream|connection reset|incomplete (read|chunked)|chunk|server-sent|unexpected eof)\b/i,
  ],
  [
    "endpoint",
    /\b(endpoint|base[ _]?url|404|dns|econnrefused|unreachable|name resolution|certificate|ssl|tls|timed out|timeout)\b/i,
  ],
  [
    "provider",
    /\b(rate[ -]?limit|429|overloaded|quota|model|provider|50[0-9]|bad gateway|service unavailable)\b/i,
  ],
  ["daemon", /\b(daemon|gateway|socket|disconnected)\b/i],
];

/** The layer for a turn's exit reason and failure text. */
export function classifyFailure(reason: string, text: string): FailureLayer {
  if (reason === "persistence_failed") return "disk";
  for (const [layer, pattern] of RULES) if (pattern.test(text)) return layer;
  if (reason === "provider_failed") return "provider";
  return "runtime";
}

/** What the card offers, most useful first. */
export function failureActions(layer: FailureLayer, retryable: boolean): FailureAction[] {
  const retry: FailureAction[] = retryable ? ["retry"] : [];
  switch (layer) {
    case "provider":
      return [...retry, "choose-model", "open-settings"];
    case "endpoint":
      return [...retry, "open-settings"];
    case "streaming":
      return ["retry"];
    case "auth":
      return ["sign-in", "open-settings"];
    case "daemon":
      return ["reconnect", "open-logs"];
    case "disk":
      return [...retry, "open-logs"];
    case "runtime":
      return [...retry, "open-logs", "new-session"];
  }
}
