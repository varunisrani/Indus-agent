// A session's stored conversation from the backend's REST API (integration of P20-18 with the
// real daemon). The daemon's WebSocket does not serve `session.history` (its attach surface is
// events; stored rows are read through `indus serve`, P17 / P18-11), so the desktop reads
// `GET /api/sessions/{id}/messages` through main's REST proxy and flattens the rows the way the
// daemon's display projection does: user, assistant and tool rows only, an assistant row that is
// only tool calls dropped, tool rows named by their call and carrying its arguments (the route
// masks them; W47-a). A backend without the route (the fake test daemon) answers not-found, and
// the caller falls back to `session.history`.

import type { HistoryMessage } from "@indus/protocol";
import type { IndusApiRequest, IndusApiResult } from "../global.d.ts";

export type ApiCall = (request: IndusApiRequest) => Promise<IndusApiResult>;

/** The REST route does not exist on this backend: use the WebSocket method instead. */
export class HistoryRouteMissing extends Error {}

interface StoredRow {
  id?: unknown;
  role?: unknown;
  content?: unknown;
  tool_calls?: unknown;
  tool_call_id?: unknown;
  tool_name?: unknown;
}

const SHOWN = new Set(["user", "assistant", "tool"]);

type Arguments = NonNullable<HistoryMessage["arguments"]>;

/** A call's arguments as an object (the route sends JSON text); null when they are not one. */
function parseArguments(raw: unknown): Arguments | null {
  let value = raw;
  if (typeof raw === "string") {
    try {
      value = raw.trim() === "" ? {} : JSON.parse(raw);
    } catch {
      return null;
    }
  }
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Arguments)
    : null;
}

/** Stored rows (oldest first) → what the transcript shows. */
export function displayRows(rows: readonly StoredRow[]): HistoryMessage[] {
  const names = new Map<string, string>();
  const calls = new Map<string, unknown>();
  const out: HistoryMessage[] = [];
  for (const row of rows) {
    const role = typeof row.role === "string" ? row.role : "";
    if (!SHOWN.has(role)) continue;
    if (Array.isArray(row.tool_calls)) {
      for (const call of row.tool_calls as Array<Record<string, unknown>>) {
        // the route flattens a call to {id, name, arguments}; a raw row nests `function.name`
        const fn = call?.function as Record<string, unknown> | undefined;
        const name = typeof call?.name === "string" ? call.name : fn?.name;
        if (typeof call?.id === "string" && typeof name === "string" && name !== "")
          names.set(call.id, name);
        if (typeof call?.id === "string") calls.set(call.id, call.arguments ?? fn?.arguments);
      }
    }
    const text = typeof row.content === "string" ? row.content : "";
    const rowId = typeof row.id === "number" ? row.id : null;
    if (role === "tool") {
      const callId = typeof row.tool_call_id === "string" ? row.tool_call_id : "";
      const name =
        (typeof row.tool_name === "string" && row.tool_name !== "" ? row.tool_name : null) ??
        names.get(callId) ??
        "tool";
      const args = parseArguments(calls.get(callId));
      out.push({
        role: "tool",
        text,
        name,
        ...(args === null ? {} : { arguments: args }),
        row_id: rowId,
      });
      continue;
    }
    if (text.trim() === "") continue; // an assistant row with only tool calls
    out.push({ role: role as "user" | "assistant", text, row_id: rowId });
  }
  return out;
}

/** Load one session's display rows for `profile` (null: the connection's own). */
export function restHistory(
  api: ApiCall,
  profile: string | null,
): (sessionId: string) => Promise<HistoryMessage[]> {
  return async (sessionId) => {
    const answer = await api({
      method: "GET",
      path: `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
      ...(profile === null ? {} : { profile }),
    });
    if (answer.kind === "not-found") throw new HistoryRouteMissing("no messages route");
    if (answer.status >= 400) throw new Error(`the history could not be read (${answer.status})`);
    const rows = (answer.body as { messages?: unknown } | null)?.messages;
    return displayRows(Array.isArray(rows) ? (rows as StoredRow[]) : []);
  };
}
